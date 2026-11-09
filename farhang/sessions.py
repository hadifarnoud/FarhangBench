"""Cookie sessions for the review page's sign-in.

A session is a random bearer token in an ``fb_session`` cookie. Only ``sha256(token)`` is kept, so the state file
on disk (``<state>/sessions.json``) is useless to anyone who steals a copy of it. Each session records who it
belongs to, when it was created, when it was last seen, when it expires, the IP and User-Agent it was made from,
and a fingerprint of the account's password hash, so a password change (managed account or edited users Secret)
ends its sessions. Sessions expire 14 days after last use and never live past 30 days from creation: the expiry
slides forward, but only when the session was last seen more than an hour ago, so the file is written at most
once an hour per session. With no path the store is in-memory only (local mode without ``--accounts``).
"""

import hashlib
import json
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

TTL = timedelta(days=14)
MAX_AGE = timedelta(days=30)  # absolute cap from creation, regardless of the sliding expiry
WRITE_AFTER = timedelta(hours=1)  # sliding renewal granularity, so the file isn't rewritten on every hit
_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _iso(dt):
    return dt.strftime(_FMT)


def _parse(text):
    return datetime.strptime(text, _FMT).replace(tzinfo=timezone.utc)


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Sessions:
    """The active sign-in sessions: ``create``, ``get``, ``revoke``, ``revoke_user`` and ``list_user``."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._sessions = {}
        if self.path and self.path.is_file():
            try:
                self._sessions = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self._sessions = {}  # a corrupt file is safer ignored than trusted
        self._purge()

    def _purge(self):
        now = datetime.now(timezone.utc)
        expired = [h for h, s in self._sessions.items()
                   if _parse(s["expires"]) <= now or _parse(s["created"]) + MAX_AGE <= now]
        if expired:
            for h in expired:
                del self._sessions[h]
            self._save()

    def _save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._sessions, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic, so a crash never leaves a half-written file

    def create(self, name, ip="", ua="", fingerprint=""):
        """Start a session for ``name`` and return the token (only its sha256 is stored). ``fingerprint`` is a
        short digest of the account's password hash (see Accounts.password_fingerprint), so the session stops
        proving identity once the password changes."""
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        with self._lock:
            self._sessions[_hash(token)] = {"name": name, "created": _iso(now), "last_seen": _iso(now),
                                            "expires": _iso(now + TTL), "ip": ip, "ua": ua[:200],
                                            "fingerprint": fingerprint}
            self._purge()
            self._save()
        return token

    def get(self, token):
        """The session this cookie belongs to (a copy of its record), or None. Seeing a session slides its
        expiry forward, never past MAX_AGE from its creation; the record's ``renewed`` then holds the seconds
        left on the new expiry (None otherwise), so the caller can re-send the cookie."""
        if not token:
            return None
        now = datetime.now(timezone.utc)
        with self._lock:
            session = self._sessions.get(_hash(token))
            if not session or _parse(session["expires"]) <= now:
                return None
            created = _parse(session["created"])
            if created + MAX_AGE <= now:  # older than any cookie could deserve; drop it
                del self._sessions[_hash(token)]
                self._save()
                return None
            renewed = None
            slide = min(now + TTL, created + MAX_AGE) - _parse(session["expires"]) > WRITE_AFTER
            session["last_seen"] = _iso(now)
            if slide:
                session["expires"] = _iso(min(now + TTL, created + MAX_AGE))
                self._save()
                renewed = max(1, int((_parse(session["expires"]) - now).total_seconds()))
            return dict(session, renewed=renewed)

    def update_fingerprint(self, token, fingerprint):
        """Point ``token``'s session at its user's new password fingerprint (after an own password change)."""
        with self._lock:
            session = self._sessions.get(_hash(token or ""))
            if session:
                session["fingerprint"] = fingerprint
                self._save()

    def revoke(self, token):
        """End one session (sign-out)."""
        with self._lock:
            if self._sessions.pop(_hash(token or ""), None) is not None:
                self._save()

    def revoke_user(self, name, except_token=None):
        """End all of ``name``'s sessions — except ``except_token`` (the current one), when given."""
        keep = _hash(except_token) if except_token else None
        with self._lock:
            gone = [h for h, s in self._sessions.items() if s["name"] == name and h != keep]
            for h in gone:
                del self._sessions[h]
            if gone:
                self._save()

    def list_user(self, name):
        """``name``'s sessions, most recently seen first (token hashes are never returned)."""
        with self._lock:
            rows = [dict(s) for s in self._sessions.values() if s["name"] == name]
        rows.sort(key=lambda s: s["last_seen"], reverse=True)
        return rows

    def count(self, name):
        """How many sessions ``name`` currently has."""
        with self._lock:
            return sum(1 for s in self._sessions.values() if s["name"] == name)
