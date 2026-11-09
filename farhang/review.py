"""A local web page for human validation of benchmark items: ``python -m farhang review``.

Each reviewer answers an item blind, then sees the answer key and judges it. The server never sends an item's
key before that reviewer's blind answer is recorded, and the first blind answer cannot be changed. Reviews are
appended to ``validation/<reviewer>.jsonl`` (see ``farhang.validation``), which are plain files to commit.

Locally, reviewers type their name. For a shared deployment (``deploy/``), pass ``--users`` (admin accounts, one
line each from ``python -m farhang review-user``) and ``--accounts`` (a writable file for the reviewer accounts that
admins manage in the People tab; see ``farhang.accounts``). Every request then needs HTTP Basic auth, and the
signed-in account is the reviewer, so nobody can answer or peek at keys under someone else's name.

The default queue is native items (the ones the FarhangScore is built from); any imported or derived source can be
reviewed too, as an audit.
"""

import io
import json
import mimetypes
import threading
import time
import webbrowser
import zipfile
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .data import ROOT, load_items
from .runner import gold_summary
from .taxonomy import NATIVE_SOURCE, TASKS, TRACKS
from .weights import review_required_sources
from .validation import (FLAGS, REVIEWS_DIR, VERDICTS, append_record, authors_of, blind_correct, content_hash,
                         item_status, load_records, merge_reviews, reviewer_slug)

_SECURITY_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "frame-ancestors 'none'"),
]
SESSION_COOKIE = "fb_session"
SESSION_COOKIE_MAX_AGE = 14 * 24 * 3600
UI_DIR = Path(__file__).resolve().parent / "review_ui"
_STATIC_FILES = {"app.js": "text/javascript; charset=utf-8", "app.css": "text/css; charset=utf-8"}


def index_html():
    """The reviewer page with its metadata block: tracks, tasks and flag labels for the UI to render."""
    meta = json.dumps({"tracks": {k: v[2] for k, v in TRACKS.items()}, "tasks": TASKS, "flags": FLAGS},
                      ensure_ascii=False)
    return (UI_DIR / "index.html").read_text(encoding="utf-8").replace("__META__", meta)


class _LoginAttempts:
    """In-memory rate limit for sign-in: 5 failed tries per name or 30 per IP in 15 minutes means wait."""

    WINDOW = 15 * 60
    PER_NAME, PER_IP = 5, 30

    def __init__(self):
        self._lock = threading.Lock()
        self._by_name, self._by_ip = defaultdict(list), defaultdict(list)

    def _remaining_wait(self, hits, limit, now):
        hits[:] = [t for t in hits if now - t < self.WINDOW]
        if len(hits) < limit:
            return 0
        return int(hits[-limit] + self.WINDOW - now) + 1

    def allowed(self, name, ip):
        """0 if a login try may go ahead, else how many seconds to wait before trying again."""
        with self._lock:
            now = time.time()
            return max(self._remaining_wait(self._by_name[name], self.PER_NAME, now),
                       self._remaining_wait(self._by_ip[ip], self.PER_IP, now))

    def failure(self, name, ip):
        with self._lock:
            now = time.time()
            self._by_name[name].append(now)
            self._by_ip[ip].append(now)

    def success(self, name):
        with self._lock:
            self._by_name.pop(name, None)

_lock = threading.Lock()
_BLIND_KEYS = ("id", "track", "task", "format", "lang", "context", "prompt", "options", "labels", "images")


def source_of(item):
    return item["metadata"].get("source", NATIVE_SOURCE)


def blind_view(item):
    """What a reviewer may see before answering: no key, rubric, reference or notes."""
    view = {k: item[k] for k in _BLIND_KEYS if k in item}
    view["source"] = source_of(item)
    return view


def key_view(item):
    """The answer key and everything else, shown after the blind answer."""
    keys = ("answer", "reference", "rubric", "distractor_types")
    return dict({k: item[k] for k in keys if k in item}, gold=gold_summary(item), metadata=item["metadata"],
                file=item.get("_source", ""))


class ReviewStore:
    """Items plus reviews, re-read on every call so edits made in ``farhang browse`` show up straight away."""

    def __init__(self, data_dir, reviews_dir=REVIEWS_DIR):
        self.data_dir, self.reviews_dir = Path(data_dir), Path(reviews_dir)

    def items(self):
        return {i["id"]: i for i in load_items(self.data_dir)}

    def merged(self):
        return merge_reviews(load_records(self.reviews_dir))

    def listing(self, reviewer):
        items, merged, gated = self.items(), self.merged(), review_required_sources()
        out = []
        for it in items.values():
            reviews = merged.get((it["id"], content_hash(it)), {})
            mine = reviews.get(reviewer, {})
            if it["metadata"].get("author") and reviewer_slug(it["metadata"]["author"]) == reviewer:
                continue  # authors don't validate their own items
            own_source = reviewer in authors_of(it)  # a dataset's authors may review it, but their verdict won't count
            st = item_status(it, reviews)
            out.append(dict(blind_view(it), status=st["status"], verdicts=st["verdicts"], gated=source_of(it) in gated,
                            answered="blind_answer" in mine, my_verdict=mine.get("verdict"),
                            own_source=own_source))
        order = {t: i for i, t in enumerate(TASKS)}
        out.sort(key=lambda it: (it["source"] != NATIVE_SOURCE, order.get(it["task"], len(order)), it["id"]))
        return out

    def reveal(self, reviewer, item_id):
        """The key and this reviewer's review, or None if they have not answered blind yet."""
        item = self.items().get(item_id)
        if item is None:
            return None
        mine = self.merged().get((item_id, content_hash(item)), {}).get(reviewer, {})
        if "blind_answer" not in mine:
            return None
        return dict(key_view(item), review=mine)

    def record_blind(self, reviewer, item_id, answer):
        with _lock:
            item = self.items().get(item_id)
            if item is None:
                raise KeyError(item_id)
            h = content_hash(item)
            mine = self.merged().get((item_id, h), {}).get(reviewer, {})
            if "blind_answer" not in mine:  # the first blind answer stands
                if item["format"] == "mcq" and answer is not None:
                    answer = int(answer)
                append_record(self.reviews_dir, {"type": "blind", "reviewer": reviewer, "item_id": item_id,
                                                 "item_hash": h, "answer": answer,
                                                 "correct": blind_correct(item, answer)})
        return self.reveal(reviewer, item_id)

    def record_verdict(self, reviewer, item_id, verdict, flags=(), comment="", suggested_answer=""):
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
        unknown = set(flags) - set(FLAGS)
        if unknown:
            raise ValueError(f"unknown flags: {', '.join(sorted(unknown))}")
        with _lock:
            if self.reveal(reviewer, item_id) is None:
                raise PermissionError("answer the item blind before judging its key")
            item = self.items()[item_id]
            append_record(self.reviews_dir, {"type": "verdict", "reviewer": reviewer, "item_id": item_id,
                                             "item_hash": content_hash(item), "verdict": verdict,
                                             "flags": sorted(set(flags)), "comment": comment.strip(),
                                             "suggested_answer": suggested_answer.strip()})
        return self.reveal(reviewer, item_id)

    def progress(self):
        """Status counts by track and task for native items, by source for the rest, plus open problems."""
        items, merged = self.items(), self.merged()
        rows = defaultdict(Counter)
        accuracy = defaultdict(list)
        reviewers = Counter()
        problems = []
        for it in items.values():
            reviews = merged.get((it["id"], content_hash(it)), {})
            st = item_status(it, reviews)
            group = it["task"] if source_of(it) == NATIVE_SOURCE else f"source:{source_of(it)}"
            if st["status"] not in ("retired", "outdated"):
                rows[group]["items"] += 1
            rows[group][st["status"]] += 1
            for r in reviews.values():
                if r.get("verdict"):
                    reviewers[r["reviewer"]] += 1
                if r.get("blind_correct") is not None:
                    accuracy[group].append(r["blind_correct"])
            if st["status"] == "disputed" or (st["flags"] and st["status"] not in ("retired", "outdated")):
                problems.append({"id": it["id"], "task": it["task"], "source": source_of(it), "status": st["status"],
                                 "flags": st["flags"], "prompt": it["prompt"][:160],
                                 "comments": [{"reviewer": r["reviewer"], "verdict": r.get("verdict"),
                                               "comment": r.get("comment", ""),
                                               "suggested_answer": r.get("suggested_answer", "")}
                                              for r in reviews.values() if r.get("verdict")]})
        gated = review_required_sources()
        table = [{"group": g, **dict(c), "review_required": g.removeprefix("source:") in gated, "human_accuracy": (sum(accuracy[g]) / len(accuracy[g])) if accuracy[g] else None,
                  "blind_answers": len(accuracy[g])} for g, c in rows.items()]
        order = list(TASKS)
        table.sort(key=lambda r: (r["group"].startswith("source:"), order.index(r["group"]) if r["group"] in order else 0,
                                  r["group"]))
        return {"rows": table, "reviewers": dict(reviewers.most_common()), "problems": problems}


def summary_lines(store):
    """Plain-text progress, for ``python -m farhang review --summary``."""
    p = store.progress()
    cols = ("items", "unreviewed", "in_review", "validated", "disputed", "outdated")
    lines = [f"{'group':40s}" + "".join(f"{c:>12s}" for c in cols) + f"{'human acc.':>12s}"]
    for r in p["rows"]:
        acc = "" if r["human_accuracy"] is None else f"{100 * r['human_accuracy']:.0f}%"
        lines.append(f"{r['group']:40s}" + "".join(f"{r.get(c, 0):>12d}" for c in cols) + f"{acc:>12s}")
    lines.append("")
    lines.append("reviewers: " + (", ".join(f"{k} ({v})" for k, v in p["reviewers"].items()) or "none yet"))
    lines.append(f"open problems (disputed or flagged): {len(p['problems'])}")
    return lines


def export_zip(reviews_dir):
    """All review files as a zip, to download from a deployment and commit to git."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(Path(reviews_dir).glob("*.jsonl")) if Path(reviews_dir).is_dir() else []:
            zf.write(file, f"validation/{file.name}")
    return buf.getvalue()


def make_handler(store, accounts=None, sessions=None, audit=None, secure_cookies=False):
    """Request handler. With ``accounts`` (a ``farhang.accounts.Accounts``) API routes need sign-in: the
    ``fb_session`` cookie from /api/login, or ``Authorization: Basic`` for scripts. Never sends ``WWW-Authenticate``,
    so browsers show the in-page login form instead of the Basic-auth popup. Nothing sends an item's key before the
    reviewer's blind answer. ``audit`` records admin and sign-in events; in-memory unless ``serve`` put it on disk.
    """
    from .accounts import AccountError, AuditLog
    from .sessions import Sessions

    sessions = sessions or Sessions()  # in-memory when the server was made without a state directory
    audit = audit if audit is not None else AuditLog()
    attempts = _LoginAttempts()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        # ---------------------------------------------------------- auth and headers

        def _session_token(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == SESSION_COOKIE:
                    return value
            return ""

        def _client_ip(self):
            forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            return forwarded or self.client_address[0]

        def _resolve_user(self):
            """The signed-in account dict (+ the session token as ``_token``), or None."""
            if accounts is None:
                return {}
            token = self._session_token()
            if token:
                name = sessions.get(token)
                if name:
                    user = accounts.info(name)
                    if user:
                        user["_token"] = token
                        return user
                    sessions.revoke(token)  # account gone or disabled; drop the cookie's session
            return accounts.authenticate(self.headers.get("Authorization"))

        def _user(self):
            """The signed-in account, or None after sending a 401. Never sends WWW-Authenticate (popup trigger)."""
            user = self._resolve_user()
            if user is None:
                self._json(401, {"error": "sign in required"})
            return user

        def _cookie_headers(self, token=None):
            value = f"{SESSION_COOKIE}={token}" if token else f"{SESSION_COOKIE}=; Max-Age=0"
            parts = [value, "HttpOnly", "SameSite=Strict", "Path=/"]
            if token:
                parts.append(f"Max-Age={SESSION_COOKIE_MAX_AGE}")
            if secure_cookies or self.headers.get("X-Forwarded-Proto") == "https":
                parts.append("Secure")
            return [("Set-Cookie", "; ".join(parts))]

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=(), cache="no-store"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if cache:
                self.send_header("Cache-Control", cache)
            for header, value in [*_SECURITY_HEADERS, *extra]:
                self.send_header(header, value)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, obj, extra=()):
            self._send(code, json.dumps(obj, ensure_ascii=False), extra=extra)

        # ---------------------------------------------------------- sign-in endpoints

        def _whoami(self, user):
            if accounts is None:
                return {"signed_in": False, "auth_enabled": False, "reviewer": None, "role": "admin",
                        "must_change": False, "can_change_password": False, "local": True}
            if not user:
                return {"signed_in": False, "auth_enabled": True, "reviewer": None, "role": None,
                        "must_change": False, "can_change_password": False, "local": False}
            return {"signed_in": True, "auth_enabled": True, "reviewer": user["name"], "role": user.get("role"),
                    "must_change": user.get("must_change", False), "can_change_password": user.get("managed", False),
                    "local": False, "display_name": user.get("display_name", "")}

        def _login(self, body):
            if accounts is None:
                return self._json(400, {"error": "this server has no accounts; sign-in isn't used"})
            name = reviewer_slug(str(body.get("name", "")))
            ip = self._client_ip()
            wait = attempts.allowed(name, ip)
            if wait:
                return self._json(429, {"error": "too many failed tries; wait and try again", "retry_after": wait})
            user = accounts.verify_credentials(name, str(body.get("password", ""))) if name else None
            if not user:
                attempts.failure(name, ip)
                audit.record(name, "login_failed", name)
                return self._json(401, {"error": "wrong name or password"})
            attempts.success(name)
            accounts.record_login(name)
            audit.record(name, "login", name)
            token = sessions.create(name, ip, self.headers.get("User-Agent", ""))
            return self._json(200, {"ok": True, "user": self._whoami(user)}, extra=self._cookie_headers(token))

        def _logout(self):
            token = self._session_token()
            if token:
                name = sessions.get(token)
                sessions.revoke(token)
                if name:
                    audit.record(name, "logout", name)
            self._json(200, {"ok": True}, extra=self._cookie_headers(None))

        # ---------------------------------------------------------- routes

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/healthz":
                return self._send(200, "ok", "text/plain; charset=utf-8")
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/api/whoami":
                return self._json(200, self._whoami(self._resolve_user()))
            if url.path == "/":  # the page itself is public; its data routes are not
                return self._send(200, index_html(), "text/html; charset=utf-8")
            if url.path.startswith("/static/"):
                name = url.path[len("/static/"):]
                if name in _STATIC_FILES and (UI_DIR / name).is_file():
                    return self._send(200, (UI_DIR / name).read_bytes(), _STATIC_FILES[name], cache="no-cache")
                return self._json(404, {"error": "not found"})
            user = self._user()
            if user is None:
                return
            reviewer = user.get("name") or reviewer_slug(qs.get("reviewer", ""))
            if user.get("must_change"):
                return self._json(403, {"error": "choose a new password first", "must_change": True})
            if url.path in ("/api/users", "/api/admin/users"):
                if user.get("role") != "admin":
                    return self._json(403, {"error": "admins only"})
                return self._json(200, accounts.list())
            if url.path == "/api/admin/audit":
                if user.get("role") != "admin":
                    return self._json(403, {"error": "admins only"})
                return self._json(200, audit.query(limit=int(qs.get("limit", 50)), offset=int(qs.get("offset", 0)),
                                                   actor=qs.get("actor") or None, action=qs.get("action") or None))
            if url.path == "/api/export":
                data = export_zip(store.reviews_dir)
                return self._send(200, data, "application/zip",
                                  extra=[("Content-Disposition", 'attachment; filename="farhangbench-reviews.zip"')])
            if url.path == "/api/items":
                if not reviewer:
                    return self._json(400, {"error": "reviewer name required"})
                return self._json(200, store.listing(reviewer))
            if url.path == "/api/key":
                key = store.reveal(reviewer, qs.get("id", ""))
                return self._json(200, key) if key else self._json(403, {"error": "answer the item blind first"})
            if url.path == "/api/progress":
                return self._json(200, store.progress())
            if url.path.startswith("/data/images/"):
                file = (ROOT / url.path.lstrip("/")).resolve()
                if (ROOT / "data" / "images").resolve() in file.parents and file.is_file():
                    return self._send(200, file.read_bytes(),
                                      mimetypes.guess_type(file.name)[0] or "application/octet-stream")
            self._json(404, {"error": "not found"})

        def do_POST(self):
            origin = self.headers.get("Origin")
            if origin is not None and urlparse(origin).netloc != self.headers.get("Host"):
                return self._json(403, {"error": "cross-origin request refused"})
            # A JSON content type can't be sent by a plain cross-site form, which backs up the origin check.
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json(415, {"error": "send JSON"})
            url = urlparse(self.path)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "bad request: the body must be JSON"})
            if url.path == "/api/login":
                return self._login(body if isinstance(body, dict) else {})
            if url.path == "/api/logout":
                return self._logout()
            user = self._user()
            if user is None:
                return
            try:
                if url.path == "/api/password":
                    if not user.get("managed"):
                        raise AccountError("this account comes from the users Secret; change it there")
                    current = str(body.get("current_password", ""))
                    if not user.get("must_change") and not accounts.check_password(user["name"], current):
                        raise AccountError("the current password is wrong")
                    accounts.change_password(user["name"], str(body.get("new_password", "")))
                    sessions.revoke_user(user["name"], except_token=user.get("_token"))
                    audit.record(user["name"], "password_change", user["name"])
                    return self._json(200, {"ok": True})
                if user.get("must_change"):
                    return self._json(403, {"error": "choose a new password first", "must_change": True})
                if url.path in ("/api/users", "/api/admin/users"):
                    if user.get("role") != "admin":
                        return self._json(403, {"error": "admins only"})
                    action, name = body.get("action"), reviewer_slug(body.get("name", ""))
                    if action == "add":
                        name, password = accounts.add(
                            body.get("name", ""), body.get("role", "reviewer"), user["name"],
                            password=body.get("password"), display_name=str(body.get("display_name", "")),
                            must_change=body.get("must_change") if "must_change" in body else None)
                        return self._json(200, {"name": name, "password": password})
                    if action == "reset":
                        return self._json(200, {"name": name, "password": accounts.reset(name, user["name"])})
                    if action == "role":
                        accounts.set_role(name, body.get("role"), user["name"])
                        return self._json(200, {"ok": True})
                    if action in ("disable", "enable"):
                        accounts.set_disabled(name, action == "disable", user["name"])
                        return self._json(200, {"ok": True})
                    if action == "rename":
                        accounts.set_display_name(name, str(body.get("display_name", "")), user["name"])
                        return self._json(200, {"ok": True})
                    if action == "remove":
                        accounts.remove(name, user["name"])
                        return self._json(200, {"ok": True})
                    raise ValueError("action must be add, reset, role, disable, enable, remove or rename")
                reviewer = user.get("name") or reviewer_slug(body.get("reviewer", ""))
                if not reviewer:
                    raise ValueError("reviewer name required")
                if url.path == "/api/blind":
                    return self._json(200, store.record_blind(reviewer, body["item_id"], body.get("answer")))
                if url.path == "/api/verdict":
                    return self._json(200, store.record_verdict(
                        reviewer, body["item_id"], body.get("verdict"), body.get("flags", []),
                        body.get("comment", ""), body.get("suggested_answer", "")))
                return self._json(404, {"error": "not found"})
            except AccountError as exc:
                return self._json(400, {"error": str(exc)})
            except PermissionError as exc:
                return self._json(403, {"error": str(exc)})
            except (ValueError, KeyError, TypeError) as exc:
                return self._json(400, {"error": f"bad request: {exc}"})

    return Handler


def serve(data_dir, reviews_dir=REVIEWS_DIR, host="127.0.0.1", port=8766, open_browser=True, users_file=None,
          accounts_file=None, secure_cookies=False):
    from .accounts import AccountError, Accounts, AuditLog
    from .sessions import Sessions
    store = ReviewStore(data_dir, reviews_dir)
    accounts = Accounts(accounts_file, users_file) if (users_file or accounts_file) else None
    state_dir = Path(accounts_file).parent if accounts_file else None  # the volume next to accounts.json
    sessions = Sessions(state_dir / "sessions.json" if state_dir else None)
    audit = AuditLog(state_dir / "audit.jsonl" if state_dir else None)
    if accounts is not None:
        accounts.sessions, accounts.audit = sessions, audit
        try:
            accounts.check_startup()
        except AccountError as exc:
            raise SystemExit(str(exc))
    elif host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: serving beyond localhost without --users; anyone who can reach it can review as anyone")
    server = ThreadingHTTPServer((host, port), make_handler(store, accounts, sessions, audit, secure_cookies))
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"FarhangBench review at {url} — reviews are saved to {reviews_dir}"
          f"{' — sign-in required' if accounts else ''} (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
