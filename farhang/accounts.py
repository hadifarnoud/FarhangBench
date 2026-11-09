"""Reviewer accounts managed from the review page's People tab, plus the admin audit log.

Two kinds of account:

- **Bootstrap admins** come from a read-only users file (``--users``, usually a Kubernetes Secret), one
  ``name:hash`` line each from ``python -m farhang review-user``. They can't be changed from the page; their
  ``last_login`` is noted under ``bootstrap_meta`` in the accounts file.
- **Managed accounts** live in a JSON file on the persistent volume (``--accounts``). Admins add, reset, re-role,
  disable and remove them in the page. A new or reset account gets a one-time password (or one the admin chose)
  and must choose its own at the next sign-in, unless the admin said otherwise.

Each managed account records ``hash, role, must_change, created, created_by, disabled, display_name`` (optional,
may be Persian), ``last_login`` and ``password_changed``; missing fields take defaults, so old account files keep
working. Removing an account never deletes the reviews it wrote.

Account changes also end the user's sessions (through the ``sessions`` handle the server wires in) and are written
to the audit log (``<state>/audit.jsonl``; see ``AuditLog``).
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
# A real-cost stand-in hash, so a failed sign-in for an unknown or disabled name takes as long as a real one
# and timing can't reveal which names exist.
_DUMMY_HASH = hash_password("dummy password")


class AccountError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AuditLog:
    """Append-only JSONL audit trail of admin and sign-in events. In memory when there's no state directory."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._memory = []

    def record(self, actor, action, target="", detail=""):
        row = {"time": _now(), "actor": actor, "action": action, "target": target, "detail": detail}
        with self._lock:
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            else:
                self._memory.append(row)

    def query(self, limit=50, offset=0, actor=None, action=None):
        """``{total, rows}``, newest first, optionally filtered by actor or action."""
        with self._lock:
            if self.path and self.path.is_file():
                rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()
                        if line.strip()]
            else:
                rows = list(self._memory)
        rows = [r for r in rows if (not actor or r.get("actor") == actor) and (not action or r.get("action") == action)]
        rows.reverse()
        return {"total": len(rows), "rows": rows[offset:offset + limit]}


class Accounts:
    def __init__(self, store_path=None, bootstrap_path=None):
        self.store_path = Path(store_path) if store_path else None
        self.bootstrap = Users(bootstrap_path) if bootstrap_path else None
        self._lock = threading.Lock()
        self._verified = {}  # sha256(Authorization) -> (name, hash it was checked against)
        self.sessions = None  # the server wires in its Sessions, to end sessions when accounts change
        self.audit = AuditLog()  # the server replaces this with one backed by <state>/audit.jsonl

    # ------------------------------------------------------------ storage

    def _bootstrap_users(self):
        return self.bootstrap._load() if self.bootstrap else {}

    def _read_data(self):
        if not self.store_path or not self.store_path.is_file():
            return {}
        return json.loads(self.store_path.read_text(encoding="utf-8"))

    def _read(self):
        return self._read_data().get("users", {})

    def _write_data(self, data):
        if not self.store_path:
            raise AccountError("no accounts file configured (--accounts)")
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.store_path)  # atomic, so a crash never leaves a half-written file

    def _write(self, users):
        data = self._read_data()
        data["users"] = users
        self._write_data(data)

    def _revoke(self, name):
        if self.sessions:
            self.sessions.revoke_user(name)

    def check_startup(self):
        """Raise AccountError unless at least one admin can sign in."""
        try:
            admins = list(self._bootstrap_users())
        except OSError as exc:
            raise AccountError(f"cannot read the users file {self.bootstrap.path}: {exc}") from exc
        admins += [n for n, u in self._read().items() if u.get("role") == "admin" and not u.get("disabled")]
        if not admins:
            raise AccountError("no admin account: put at least one line from `farhang review-user` in --users")

    # ------------------------------------------------------------ sign-in

    def _lookup(self, name):
        """(stored password hash, account info dict) for ``name``, or None: unknown, disabled or no such account."""
        boot = self._bootstrap_users()
        if name in boot:
            info = {"name": name, "role": "admin", "must_change": False, "managed": False, "display_name": ""}
            return boot[name], info
        user = self._read().get(name)
        if not user or user.get("disabled"):
            return None
        info = {"name": name, "role": user.get("role", "reviewer"), "must_change": bool(user.get("must_change")),
                "managed": True, "display_name": user.get("display_name", "")}
        return user["hash"], info

    def info(self, name):
        """The account info dict for ``name`` (used to resolve a session cookie), or None."""
        found = self._lookup(name)
        return found[1] if found else None

    def password_fingerprint(self, name):
        """A short digest of the account's stored password hash, or None if there's no enabled account. A
        session keeps the fingerprint from sign-in; when this changes (new password, Secret edited) the
        session no longer proves identity."""
        found = self._lookup(name)
        if not found:
            return None
        return hashlib.sha256(found[0].encode("utf-8")).hexdigest()[:16]

    def verify_credentials(self, name, password):
        """The account info dict for a correct name+password pair, else None (same for every failure)."""
        found = self._lookup(reviewer_slug(name))
        encoded = found[0] if found else _DUMMY_HASH
        if verify_password(password, encoded) and found:
            return found[1]
        return None

    def check_password(self, name, password):
        """Whether ``password`` is ``name``'s current password (managed accounts only)."""
        user = self._read().get(name)
        return bool(user) and verify_password(password, user["hash"])

    def authenticate(self, authorization):
        """{'name', 'role', 'must_change', 'managed', ...} for valid Basic credentials, else None."""
        import base64
        if not authorization or not authorization.startswith("Basic "):
            return None
        try:
            name, password = base64.b64decode(authorization[6:]).decode("utf-8").split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return None
        name = reviewer_slug(name)
        found = self._lookup(name)
        encoded = found[0] if found else _DUMMY_HASH  # unknown names get the dummy, keeping the timing even
        token = hashlib.sha256(authorization.encode("utf-8")).hexdigest()
        # PBKDF2 is slow on purpose; skip it for credentials already checked against the same stored hash.
        if found and self._verified.get(token) == (name, encoded):
            return found[1]
        if verify_password(password, encoded) and found:
            self._verified[token] = (name, encoded)
            return found[1]
        return None

    def record_login(self, name):
        """Note a successful sign-in: ``last_login`` on a managed account, under ``bootstrap_meta`` otherwise."""
        if not self.store_path:
            return
        with self._lock:
            data = self._read_data()
            users = data.get("users", {})
            if name in users:
                users[name]["last_login"] = _now()
            else:
                data.setdefault("bootstrap_meta", {}).setdefault(name, {})["last_login"] = _now()
            self._write_data(data)

    # ------------------------------------------------------------ management (admins)

    def list(self):
        """Every account (bootstrap and managed), with display name, status, last sign-in and live session count."""
        meta = self._read_data().get("bootstrap_meta", {})

        def sessions_of(name):
            return self.sessions.count(name) if self.sessions else 0

        out = [{"name": n, "role": "admin", "managed": False, "must_change": False, "created": None,
                "created_by": None, "disabled": False, "display_name": "", "last_login": meta.get(n, {}).get("last_login"),
                "active_sessions": sessions_of(n)} for n in sorted(self._bootstrap_users())]
        for n, u in sorted(self._read().items()):
            out.append({"name": n, "role": u.get("role", "reviewer"), "managed": True,
                        "must_change": bool(u.get("must_change")), "created": u.get("created"),
                        "created_by": u.get("created_by"), "disabled": bool(u.get("disabled")),
                        "display_name": u.get("display_name", ""), "last_login": u.get("last_login"),
                        "active_sessions": sessions_of(n)})
        return out

    def _guard_last_admin(self, name, users, verb):
        """Refuse ``verb`` when it would leave no enabled admin (bootstrap admins count)."""
        admins = set(self._bootstrap_users())
        admins |= {n for n, u in users.items() if u.get("role") == "admin" and not u.get("disabled")}
        if name in admins and len(admins) <= 1:
            raise AccountError(f"can't {verb} the last admin account")

    def add(self, name, role, by, password=None, display_name="", must_change=None):
        """Create an account. Returns (sign-in name, one-time password) — the password is None when the admin
        chose one. A chosen password (10+ characters) still means must_change, unless the admin overrode it."""
        slug = reviewer_slug(name)
        if not slug:
            raise AccountError("the name needs at least one letter or digit")
        if role not in ROLES:
            raise AccountError(f"role must be one of {', '.join(ROLES)}")
        if password is not None and len(password) < MIN_PASSWORD:
            raise AccountError(f"use at least {MIN_PASSWORD} characters")
        if isinstance(must_change, bool) or must_change is None:
            override = must_change
        elif isinstance(must_change, str) and must_change in ("true", "false"):
            override = must_change == "true"
        else:
            raise AccountError('must_change must be a boolean (false, true, "false" or "true")')
        with self._lock:
            users = self._read()
            if slug in users or slug in self._bootstrap_users():
                raise AccountError(f"{slug} already has an account")
            chosen = password is not None
            password = password if chosen else secrets.token_urlsafe(9)
            users[slug] = {"hash": hash_password(password), "role": role,
                           "must_change": override if (chosen and override is not None) else True,
                           "created": _now(), "created_by": by, "disabled": False,
                           "display_name": display_name or "", "last_login": None, "password_changed": None}
            self._write(users)
        self.audit.record(by, "add", slug, role)
        return slug, (None if chosen else password)

    def reset(self, name, by):
        """Give ``name`` a new one-time password and end their sessions. The old password stops working."""
        with self._lock:
            users = self._managed(name)
            password = secrets.token_urlsafe(9)
            users[name].update(hash=hash_password(password), must_change=True, reset=_now(), reset_by=by,
                               password_changed=None)
            self._write(users)
        self._revoke(name)
        self.audit.record(by, "reset", name)
        return password

    def set_role(self, name, role, by):
        if role not in ROLES:
            raise AccountError(f"role must be one of {', '.join(ROLES)}")
        if name == by:
            raise AccountError("you can't change your own role")
        with self._lock:
            users = self._managed(name)
            if role != "admin":
                self._guard_last_admin(name, users, "demote")
            users[name]["role"] = role
            self._write(users)
        self._revoke(name)  # their new role takes effect on the next sign-in
        self.audit.record(by, "role", name, role)

    def set_disabled(self, name, disabled, by):
        """Disable or re-enable an account. A disabled account can't sign in (cookie, password or Basic) and its
        sessions end at once."""
        if name == by:
            raise AccountError("you can't disable your own account")
        with self._lock:
            users = self._managed(name)
            if disabled:
                self._guard_last_admin(name, users, "disable")
            users[name]["disabled"] = bool(disabled)
            self._write(users)
        self._revoke(name)
        self.audit.record(by, "disable" if disabled else "enable", name)

    def set_display_name(self, name, display_name, by):
        """Set the optional display name (free text, may be Persian) shown instead of the sign-in name."""
        with self._lock:
            users = self._managed(name)
            users[name]["display_name"] = display_name or ""
            self._write(users)
        self.audit.record(by, "rename", name, display_name or "")

    def remove(self, name, by):
        if name == by:
            raise AccountError("you can't remove your own account")
        with self._lock:
            users = self._managed(name)
            self._guard_last_admin(name, users, "remove")
            del users[name]
            self._write(users)
        self._revoke(name)
        self.audit.record(by, "remove", name)

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
            users[name].update(hash=hash_password(new_password), must_change=False, password_changed=_now())
            self._write(users)
