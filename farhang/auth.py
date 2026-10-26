"""Reviewer accounts for a shared deployment of the review page (HTTP Basic auth over TLS).

A users file has one ``name:hash`` line per reviewer, where the hash comes from ``python -m farhang review-user``.
Lines starting with ``#`` are ignored. The file is re-read when it changes, so accounts can be added by updating
the Kubernetes Secret it is mounted from, without a restart.
"""

import base64
import hashlib
import hmac
import os
import secrets
import threading
from pathlib import Path

from .validation import reviewer_slug

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000


def hash_password(password, salt=None, iterations=ITERATIONS):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), iterations)
    return f"{ALGORITHM}${iterations}${salt}${base64.b64encode(digest).decode('ascii')}"


def verify_password(password, encoded):
    try:
        algorithm, iterations, salt, _ = encoded.split("$", 3)
        if algorithm != ALGORITHM:
            return False
        return hmac.compare_digest(hash_password(password, salt, int(iterations)), encoded)
    except (ValueError, TypeError):
        return False


def user_line(name, password):
    slug = reviewer_slug(name)
    if not slug:
        raise ValueError("the name needs at least one letter or digit")
    return f"{slug}:{hash_password(password)}"


class Users:
    """The accounts in a users file; ``check`` returns the reviewer name for valid credentials, else None."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp, self._users, self._ok = None, {}, {}

    def _load(self):
        stamp = os.stat(self.path).st_mtime_ns
        if stamp != self._stamp:
            users = {}
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and ":" in line:
                    name, encoded = line.split(":", 1)
                    users[reviewer_slug(name)] = encoded.strip()
            self._stamp, self._users, self._ok = stamp, users, {}
        return self._users

    def check(self, authorization):
        """Parse a ``Basic`` Authorization header and verify it."""
        if not authorization or not authorization.startswith("Basic "):
            return None
        try:
            name, password = base64.b64decode(authorization[6:]).decode("utf-8").split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return None
        name = reviewer_slug(name)
        with self._lock:
            users = self._load()
            # PBKDF2 is slow on purpose; remember credentials that already passed so each request doesn't pay it.
            token = hashlib.sha256(authorization.encode("utf-8")).hexdigest()
            if self._ok.get(name) == token:
                return name
        if name in users and verify_password(password, users[name]):
            with self._lock:
                self._ok[name] = token
            return name
        return None
