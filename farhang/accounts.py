"""Reviewer accounts managed from the review page's People tab.

Two kinds of account:

- **Bootstrap admins** come from a read-only users file (``--users``, usually a Kubernetes Secret), one
  ``name:hash`` line each from ``python -m farhang review-user``. They can't be changed from the page.
- **Managed accounts** live in a JSON file on the persistent volume (``--accounts``). Admins add, reset, re-role
  and remove them in the page. A new or reset account gets a one-time password and must choose its own at the
  next sign-in.

Removing an account never deletes the reviews it wrote.
"""

import hashlib
import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from .auth import Users, hash_password, verify_password
from .validation import reviewer_slug

ROLES = ("reviewer", "admin")
MIN_PASSWORD = 10


class AccountError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Accounts:
    def __init__(self, store_path=None, bootstrap_path=None):
        self.store_path = Path(store_path) if store_path else None
        self.bootstrap = Users(bootstrap_path) if bootstrap_path else None
        self._lock = threading.Lock()
        self._verified = {}  # sha256(Authorization) -> (name, hash it was checked against)

    # ------------------------------------------------------------ storage

    def _bootstrap_users(self):
        return self.bootstrap._load() if self.bootstrap else {}

    def _read(self):
        if not self.store_path or not self.store_path.is_file():
            return {}
        return json.loads(self.store_path.read_text(encoding="utf-8")).get("users", {})

    def _write(self, users):
        if not self.store_path:
            raise AccountError("no accounts file configured (--accounts)")
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"users": users}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.store_path)  # atomic, so a crash never leaves a half-written file

    def check_startup(self):
        """Raise AccountError unless at least one admin can sign in."""
        try:
            admins = list(self._bootstrap_users())
        except OSError as exc:
            raise AccountError(f"cannot read the users file {self.bootstrap.path}: {exc}") from exc
        admins += [n for n, u in self._read().items() if u.get("role") == "admin"]
        if not admins:
            raise AccountError("no admin account: put at least one line from `farhang review-user` in --users")

    # ------------------------------------------------------------ sign-in

    def authenticate(self, authorization):
        """{'name', 'role', 'must_change', 'managed'} for valid Basic credentials, else None."""
        import base64
        if not authorization or not authorization.startswith("Basic "):
            return None
        try:
            name, password = base64.b64decode(authorization[6:]).decode("utf-8").split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return None
        name = reviewer_slug(name)
        boot = self._bootstrap_users()
        if name in boot:
            encoded, info = boot[name], {"name": name, "role": "admin", "must_change": False, "managed": False}
        else:
            user = self._read().get(name)
            if not user:
                return None
            encoded = user["hash"]
            info = {"name": name, "role": user.get("role", "reviewer"), "must_change": bool(user.get("must_change")),
                    "managed": True}
        token = hashlib.sha256(authorization.encode("utf-8")).hexdigest()
        # PBKDF2 is slow on purpose; skip it for credentials already checked against the same stored hash.
        if self._verified.get(token) == (name, encoded) or verify_password(password, encoded):
            self._verified[token] = (name, encoded)
            return info
        return None

    # ------------------------------------------------------------ management (admins)

    def list(self):
        out = [{"name": n, "role": "admin", "managed": False, "must_change": False, "created": None, "created_by": None}
               for n in sorted(self._bootstrap_users())]
        for n, u in sorted(self._read().items()):
            out.append({"name": n, "role": u.get("role", "reviewer"), "managed": True,
                        "must_change": bool(u.get("must_change")), "created": u.get("created"),
                        "created_by": u.get("created_by")})
        return out

    def add(self, name, role, by):
        slug = reviewer_slug(name)
        if not slug:
            raise AccountError("the name needs at least one letter or digit")
        if role not in ROLES:
            raise AccountError(f"role must be one of {', '.join(ROLES)}")
        with self._lock:
            users = self._read()
            if slug in users or slug in self._bootstrap_users():
                raise AccountError(f"{slug} already has an account")
            password = secrets.token_urlsafe(9)
            users[slug] = {"hash": hash_password(password), "role": role, "must_change": True,
                           "created": _now(), "created_by": by}
            self._write(users)
        return slug, password

    def reset(self, name, by):
        with self._lock:
            users = self._managed(name)
            password = secrets.token_urlsafe(9)
            users[name].update(hash=hash_password(password), must_change=True, reset=_now(), reset_by=by)
            self._write(users)
        return password

    def set_role(self, name, role, by):
        if role not in ROLES:
            raise AccountError(f"role must be one of {', '.join(ROLES)}")
        if name == by:
            raise AccountError("you can't change your own role")
        with self._lock:
            users = self._managed(name)
            users[name]["role"] = role
            self._write(users)

    def remove(self, name, by):
        if name == by:
            raise AccountError("you can't remove your own account")
        with self._lock:
            users = self._managed(name)
            del users[name]
            self._write(users)

    def _managed(self, name):
        users = self._read()
        if name in self._bootstrap_users():
            raise AccountError(f"{name} comes from the users Secret; change it there")
        if name not in users:
            raise AccountError(f"no account called {name}")
        return users

    # ------------------------------------------------------------ self-service

    def change_password(self, name, new_password):
        if len(new_password or "") < MIN_PASSWORD:
            raise AccountError(f"use at least {MIN_PASSWORD} characters")
        with self._lock:
            users = self._managed(name)
            if verify_password(new_password, users[name]["hash"]):
                raise AccountError("choose a password different from the current one")
            users[name].update(hash=hash_password(new_password), must_change=False, changed=_now())
            self._write(users)
