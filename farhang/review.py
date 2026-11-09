"""A local web page for human validation of benchmark items: ``python -m farhang review``.

Each reviewer answers an item blind, then sees the answer key and judges it. The server never sends an item's
key before that reviewer's blind answer is recorded — except the admin audit view of an item
(``GET /api/admin/items/<id>``), which records and audits a "peek" after which that admin's blind answer is
refused. The first blind answer cannot be changed. Reviews are appended to ``validation/<reviewer>.jsonl`` (see
``farhang.validation``), which are plain files to commit.

Locally, reviewers type their name and everyone can use the admin views. For a shared deployment (``deploy/``),
pass ``--users`` (admin accounts, one line each from ``python -m farhang review-user``) and ``--accounts`` (a
writable file for the reviewer accounts that admins manage in the People tab; see ``farhang.accounts``). Sign-in
is then a cookie session from the in-page login form (``farhang.sessions``, cookie ``fb_session``); HTTP Basic
auth still works for scripts, and ``WWW-Authenticate`` is never sent, so browsers can't show the Basic-auth
popup. The signed-in account is the reviewer, so nobody can answer or peek at keys under someone else's name.

The page and its assets come from ``farhang/review_ui/``: ``GET /`` serves ``index.html`` with the ``__META__``
block filled in, and ``GET /static/<name>`` serves its JavaScript and CSS (whitelist, ``no-cache``).

The default queue is native items (the ones the FarhangScore is built from); any imported or derived source can be
reviewed too, as an audit.
"""

import base64
import io
import json
import mimetypes
import threading
import time
import webbrowser
import zipfile
from collections import Counter, defaultdict
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

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


class Peeks:
    """Admins who looked at an item's key in the audit view, as ``{name: [item_id@hash, ...]}``: seeing a key
    there means that reviewer's answer to that version of the item can no longer count as blind. In memory when
    there's no state directory (local mode)."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._peeks = {}
        if self.path and self.path.is_file():
            try:
                self._peeks = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self._peeks = {}

    def _save(self):
        if not self.path:
            return
        import os
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._peeks, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def has(self, name, key):
        with self._lock:
            return key in self._peeks.get(name, ())

    def add(self, name, key):
        """Record the peek; returns False if it was already recorded."""
        with self._lock:
            seen = self._peeks.setdefault(name, [])
            if key in seen:
                return False
            seen.append(key)
            self._save()
            return True


class _LoginAttempts:
    """In-memory rate limit for sign-in: failures inside WINDOW mean wait, with three limits — 5 tries per
    (name, IP) pair so an attacker can't lock an account out for everyone, 30 per IP, and a global cap of
    100 per name so a distributed spray still hits a wall. The counters are pruned of expired entries on
    every call and capped at MAX_KEYS keys, so arbitrary names and IPs can't grow them without bound."""

    WINDOW = 15 * 60
    PER_NAME_IP, PER_IP, PER_NAME = 5, 30, 100
    AUDIT_AFTER = 60  # at most one login_failed audit row per name per minute
    MAX_KEYS = 10_000

    def __init__(self):
        self._lock = threading.Lock()
        self._by_name_ip, self._by_ip, self._by_name = defaultdict(list), defaultdict(list), defaultdict(list)
        self._audited = {}

    def _prune(self, now):
        """Drop expired hits in every counter dict, then whole keys past MAX_KEYS (least recent first)."""
        for d in (self._by_name_ip, self._by_ip, self._by_name):
            for key in [k for k, hits in d.items() if not hits or now - hits[-1] >= self.WINDOW]:
                d.pop(key, None)
            if len(d) > self.MAX_KEYS:
                for key in sorted(d, key=lambda k: d[k][-1] if d[k] else 0)[:len(d) - self.MAX_KEYS]:
                    d.pop(key, None)
        for key in [k for k, t in self._audited.items() if now - t >= self.AUDIT_AFTER]:
            self._audited.pop(key, None)
        if len(self._audited) > self.MAX_KEYS:
            for key in sorted(self._audited, key=self._audited.get)[:len(self._audited) - self.MAX_KEYS]:
                self._audited.pop(key, None)

    def _remaining_wait(self, hits, limit, now):
        hits[:] = [t for t in hits if now - t < self.WINDOW]
        if len(hits) < limit:
            return 0
        return int(hits[-limit] + self.WINDOW - now) + 1

    def allowed(self, name, ip):
        """0 if a login try may go ahead, else how many seconds to wait before trying again."""
        with self._lock:
            now = time.time()
            self._prune(now)
            return max(self._remaining_wait(self._by_name_ip[(name, ip)], self.PER_NAME_IP, now),
                       self._remaining_wait(self._by_ip[ip], self.PER_IP, now),
                       self._remaining_wait(self._by_name[name], self.PER_NAME, now))

    def failure(self, name, ip):
        """Note a failed try; True when a ``login_failed`` audit row should be written for it (throttled)."""
        with self._lock:
            now = time.time()
            self._prune(now)
            self._by_name_ip[(name, ip)].append(now)
            self._by_ip[ip].append(now)
            self._by_name[name].append(now)
            if now - self._audited.get(name, 0) >= self.AUDIT_AFTER:
                self._audited[name] = now
                return True
            return False

    def success(self, name, ip=""):
        with self._lock:
            self._by_name_ip.pop((name, ip), None)
            self._by_name.pop(name, None)
            self._audited.pop(name, None)

_lock = threading.Lock()
_BLIND_KEYS = ("id", "track", "task", "format", "lang", "context", "prompt", "options", "labels", "images")


class _LoginBlocked(Exception):
    """A sign-in try (form or Basic) that hit the rate limit; the handler answers 429."""

    def __init__(self, wait):
        super().__init__(wait)
        self.wait = wait


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
    """Items plus reviews, re-read on every call so edits made in ``farhang browse`` show up straight away.
    ``peeks`` holds the admins who saw an item's key in the audit view (see ``Peeks``), whose blind answers can no
    longer count for those versions."""

    def __init__(self, data_dir, reviews_dir=REVIEWS_DIR, peeks=None):
        self.data_dir, self.reviews_dir, self.peeks = Path(data_dir), Path(reviews_dir), peeks

    def items(self):
        return {i["id"]: i for i in load_items(self.data_dir)}

    def merged(self):
        return merge_reviews(load_records(self.reviews_dir))

    def _peeked(self, reviewer, item_id, item_hash):
        return bool(self.peeks) and self.peeks.has(reviewer, f"{item_id}@{item_hash}")

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
                            my_flags=mine.get("flags", []),
                            my_time=max(mine.get("blind_time", ""), mine.get("verdict_time", "")) or None,
                            peeked=self._peeked(reviewer, it["id"], content_hash(it)),
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
                if self._peeked(reviewer, item_id, h):
                    raise PermissionError("you have seen this item's key in the admin view, "
                                          "so your blind answer would not be blind")
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


# ------------------------------------------------------------ admin oversight
# What the admin views show: every review record alongside the items, so an admin can check both what reviewers
# did and the current state of an item, without touching the review files.


def _hashes(items):
    return {i["id"]: content_hash(i) for i in items.values()}


def _activity_rows(store, viewer=""):
    """Every review record, newest first, with item context and whether it's about the item's current version.
    Rows about the item's CURRENT version hide other reviewers' blind ``answer``/``correct`` and their
    ``comment``/``suggested_answer`` (``hidden``) when ``viewer`` has not answered that version blind and has not
    peeked at it — otherwise the audit feed would hand out the keys without ever recording a peek."""
    items = store.items()
    hashes = _hashes(items)
    merged = store.merged()
    rows = []
    for rec in reversed(load_records(store.reviews_dir)):  # later records first, breaking equal-second ties
        it = items.get(rec["item_id"])
        current = rec.get("item_hash") == hashes.get(rec["item_id"])
        row = {"time": rec.get("time", ""), "type": rec["type"], "reviewer": rec["reviewer"],
               "item_id": rec["item_id"], "task": it["task"] if it else None,
               "source": source_of(it) if it else None, "prompt": it["prompt"][:140] if it else "",
               "current": current}
        reveal = not current or "blind_answer" in merged.get((rec["item_id"], rec["item_hash"]), {}).get(viewer, {}) \
            or (viewer and store._peeked(viewer, rec["item_id"], rec["item_hash"]))
        if not reveal:
            row["hidden"] = True
        if rec["type"] == "blind":
            if reveal:
                row.update(answer=rec.get("answer"), correct=rec.get("correct"))
        elif reveal:
            row.update(verdict=rec.get("verdict"), flags=rec.get("flags", []), comment=rec.get("comment", ""),
                       suggested_answer=rec.get("suggested_answer", ""))
        else:
            row.update(verdict=rec.get("verdict"), flags=rec.get("flags", []))
        rows.append(row)
    rows.sort(key=lambda r: r["time"], reverse=True)
    return rows


def _reviewer_rows(store, accounts):
    """Per-reviewer work summary for everyone with an account or a review record (see /api/admin/reviewers)."""
    records = load_records(store.reviews_dir)
    items = store.items()
    hashes, merged = _hashes(items), merge_reviews(records)
    acct = {u["name"]: u for u in accounts.list()} if accounts else {}
    rows = []
    for name in sorted(set(acct) | {r["reviewer"] for r in records}):
        recs = [r for r in records if r["reviewer"] == name]
        blinds = [r for r in recs if r["type"] == "blind"]
        scored = [r["correct"] for r in blinds if r.get("correct") is not None]
        counts = {"correct": 0, "wrong": 0, "unsure": 0}
        flags_raised, agreement_num, agreement_den = 0, 0, 0
        per_day = Counter(r.get("time", "")[:10] for r in recs)
        for (item_id, item_hash), reviews in merged.items():
            if hashes.get(item_id) != item_hash or name not in reviews:
                continue
            verdict = reviews[name].get("verdict")
            if not verdict:
                continue
            counts[verdict] += 1
            flags_raised += len(reviews[name].get("flags", []))
            others = [review["verdict"] for other, review in reviews.items()
                      if other != name and review.get("verdict")]
            if others:  # did they side with the majority of the other reviewers on this version?
                agreement_den += 1
                agreement_num += verdict == Counter(others).most_common(1)[0][0]
        a = acct.get(name)
        days = [date.today() - timedelta(days=d) for d in range(13, -1, -1)]
        rows.append({"name": name, "display_name": (a or {}).get("display_name", ""), "role": (a or {}).get("role"),
                     "has_account": bool(a), "blind_answers": len(blinds),
                     "verdicts": sum(1 for r in recs if r["type"] == "verdict"),
                     "blind_accuracy": (sum(scored) / len(scored)) if scored else None,
                     "verdict_counts": counts, "flags_raised": flags_raised,
                     "agreement": (agreement_num / agreement_den) if agreement_den else None,
                     "last_activity": max((r.get("time", "") for r in recs), default=None),
                     "per_day": [{"date": d.isoformat(), "n": per_day.get(d.isoformat(), 0)} for d in days]})
    return rows


def _item_rows(store, viewer=""):
    """One admin row per item: status summary plus review counts (see /api/admin/items). ``my_answered`` and
    ``peeked`` say whether ``viewer`` (the admin asking) already answered blind or saw the key, so the page knows
    when opening the audit view needs a warning first."""
    items = store.items()
    records = load_records(store.reviews_dir)
    merged = merge_reviews(records)
    last_activity = {}
    for rec in records:
        key = rec["item_id"]
        if rec.get("time", "") > last_activity.get(key, ""):
            last_activity[key] = rec.get("time", "")
    rows = []
    for it in items.values():
        reviews = merged.get((it["id"], content_hash(it)), {})
        st = item_status(it, reviews)
        rows.append({"id": it["id"], "task": it["task"], "track": it["track"], "source": source_of(it),
                     "prompt": it["prompt"][:140], "status": st["status"], "verdicts": st["verdicts"],
                     "agree": st["agree"], "flags": st["flags"], "reviewers": st["reviewers"],
                     "comments_count": sum(1 for r in reviews.values() if r.get("comment")),
                     "last_activity": last_activity.get(it["id"]),
                     "my_answered": "blind_answer" in reviews.get(viewer, {}),
                     "peeked": bool(viewer) and store._peeked(viewer, it["id"], content_hash(it))})
    rows.sort(key=lambda r: r["last_activity"] or "", reverse=True)  # stable; disputed stays on top below
    rows.sort(key=lambda r: r["status"] != "disputed")
    return rows


def _me_stats(store, accounts, reviewer):
    """One reviewer's own numbers plus their remaining queue per scope (see /api/me/stats)."""
    row = next((r for r in _reviewer_rows(store, accounts) if r["name"] == reviewer), None)
    if row is None:  # a name with no reviews and no account yet (local mode)
        days = [date.today() - timedelta(days=d) for d in range(13, -1, -1)]
        row = {"name": reviewer, "blind_answers": 0, "verdicts": 0, "blind_accuracy": None, "agreement": None,
               "per_day": [{"date": d.isoformat(), "n": 0} for d in days]}
    queue = {"native": 0, "gated": 0, "reference": 0}
    for it in store.listing(reviewer):
        if it["my_verdict"] or it["status"] in ("retired", "outdated"):
            continue
        if it["source"] == NATIVE_SOURCE:
            queue["native"] += 1
        elif it["gated"]:
            queue["gated"] += 1
        else:
            queue["reference"] += 1
    return {key: row[key] for key in ("name", "blind_answers", "verdicts", "blind_accuracy", "agreement",
                                      "per_day")} | {"queue": queue}


def _item_detail(store, item_id, peeker, audit):
    """The admin audit view of one item: blind view, key view, every reviewer's review, and old-version history.
    Looking at this page counts as a "peek": recorded, audited, and the peeker's blind answer is later refused."""
    item = store.items().get(item_id)
    if item is None:
        return None
    records = load_records(store.reviews_dir)
    item_hash = content_hash(item)
    reviews = merge_reviews(records).get((item_id, item_hash), {})
    authors = authors_of(item)
    peeked = False
    if peeker and store.peeks:
        key = f"{item_id}@{item_hash}"
        if "blind_answer" not in reviews.get(peeker, {}) and store.peeks.add(peeker, key):
            audit.record(peeker, "peek", key)
        peeked = store.peeks.has(peeker, key)
    history = sorted((dict(rec, outdated=True) for rec in records
                      if rec["item_id"] == item_id and rec.get("item_hash") != item_hash),
                     key=lambda r: r.get("time", ""), reverse=True)
    return {"item": {**blind_view(item), **key_view(item)},
            "status": item_status(item, reviews),
            "reviews": [dict(review, is_author=name in authors) for name, review in sorted(reviews.items())],
            "history": history, "peeked": peeked}


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
            # The last X-Forwarded-For entry is the one our own proxy appended; earlier ones are client-controlled.
            forwarded = [p.strip() for p in (self.headers.get("X-Forwarded-For") or "").split(",") if p.strip()]
            return forwarded[-1] if forwarded else self.client_address[0]

        def _basic_user(self):
            """HTTP Basic credentials for scripts, through the same rate limiter and audit as /api/login.
            Raises _LoginBlocked when too many tries failed; never sends WWW-Authenticate (popup trigger)."""
            header = self.headers.get("Authorization")
            if not header or not header.startswith("Basic "):
                return None
            name = ""
            try:
                name = reviewer_slug(base64.b64decode(header[6:]).decode("utf-8").split(":", 1)[0])
            except (ValueError, UnicodeDecodeError):
                pass
            wait = attempts.allowed(name, self._client_ip())
            if wait:
                raise _LoginBlocked(wait)
            user = accounts.authenticate(header)
            if not user:
                if attempts.failure(name, self._client_ip()):
                    audit.record(name, "login_failed", name)
                return None
            attempts.success(name, self._client_ip())
            return user

        def _resolve_user(self):
            """The signed-in account dict (+ the session token as ``_token``), or None."""
            if accounts is None:
                return {}
            token = self._session_token()
            if token:
                session = sessions.get(token)
                if session:
                    # The cookie proves identity only while the password hash it was made against still stands.
                    if session.get("fingerprint") != accounts.password_fingerprint(session["name"]):
                        sessions.revoke(token)
                    else:
                        user = accounts.info(session["name"])
                        if user:
                            user["_token"] = token
                            return user
                        sessions.revoke(token)  # account gone or disabled; drop the cookie's session
            return self._basic_user()

        def _rate_limited(self, wait):
            return self._json(429, {"error": "too many failed tries; wait and try again", "retry_after": wait})

        def _user(self):
            """The signed-in account, or None after sending a 401 (or 429 when rate-limited)."""
            try:
                user = self._resolve_user()
            except _LoginBlocked as exc:
                self._rate_limited(exc.wait)
                return None
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

        def _admin_ok(self, user):
            """Local mode treats everyone as an admin; with accounts it takes the admin role."""
            return accounts is None or user.get("role") == "admin"

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
                return self._rate_limited(wait)
            user = accounts.verify_credentials(name, str(body.get("password", ""))) if name else None
            if not user:
                if attempts.failure(name, ip):
                    audit.record(name, "login_failed", name)
                return self._json(401, {"error": "wrong name or password"})
            attempts.success(name, ip)
            accounts.record_login(name)
            audit.record(name, "login", name)
            token = sessions.create(name, ip, self.headers.get("User-Agent", ""),
                                    fingerprint=accounts.password_fingerprint(name))
            return self._json(200, {"ok": True, "user": self._whoami(user)}, extra=self._cookie_headers(token))

        def _logout(self):
            token = self._session_token()
            if token:
                session = sessions.get(token)
                sessions.revoke(token)
                if session:
                    audit.record(session["name"], "logout", session["name"])
            self._json(200, {"ok": True}, extra=self._cookie_headers(None))

        # ---------------------------------------------------------- routes

        def _admin(self, url, qs, user, limit, offset):
            """The admin-only oversight routes (checked by the caller)."""
            viewer = user.get("name") or reviewer_slug(qs.get("reviewer", ""))
            if url.path == "/api/admin/audit":
                return self._json(200, audit.query(limit=limit, offset=offset,
                                                   actor=qs.get("actor") or None, action=qs.get("action") or None))
            if url.path == "/api/admin/activity":
                rows = _activity_rows(store, viewer)
                rows = [r for r in rows
                        if (not qs.get("reviewer") or r["reviewer"] == qs["reviewer"])
                        and (not qs.get("type") or r["type"] == qs["type"])
                        and (not qs.get("verdict") or r.get("verdict") == qs["verdict"])
                        and (not qs.get("flagged") or r.get("flags"))
                        and (not qs.get("item") or r["item_id"] == qs["item"])
                        and (not qs.get("since") or r["time"] >= qs["since"])
                        and (not qs.get("until") or r["time"] <= qs["until"])]
                return self._json(200, {"total": len(rows), "rows": rows[offset:offset + limit]})
            if url.path == "/api/admin/reviewers":
                return self._json(200, _reviewer_rows(store, accounts))
            if url.path.startswith("/api/admin/reviewers/"):
                name = unquote(url.path[len("/api/admin/reviewers/"):])
                row = next((r for r in _reviewer_rows(store, accounts) if r["name"] == name), None)
                if row is None:
                    return self._json(404, {"error": "no such reviewer"})
                recent = [r for r in _activity_rows(store, viewer) if r["reviewer"] == name][:200]
                return self._json(200, dict(row, recent=recent))
            if url.path == "/api/admin/items":
                rows = _item_rows(store, user.get("name") or reviewer_slug(qs.get("reviewer", "")))
                q = qs.get("q", "").lower()
                rows = [r for r in rows
                        if (not qs.get("status") or r["status"] == qs["status"])
                        and (not qs.get("source") or r["source"] == qs["source"])
                        and (not qs.get("task") or r["task"] == qs["task"])
                        and (not qs.get("flag") or qs["flag"] in r["flags"])
                        and (not qs.get("reviewer") or qs["reviewer"] in r["reviewers"])
                        and (not qs.get("has_comments") or r["comments_count"] > 0)
                        and (not q or q in r["id"].lower() or q in r["prompt"].lower())]
                return self._json(200, {"total": len(rows), "rows": rows[offset:offset + limit]})
            if url.path.startswith("/api/admin/items/"):
                peeker = user.get("name") or reviewer_slug(qs.get("reviewer", ""))
                detail = _item_detail(store, unquote(url.path[len("/api/admin/items/"):]), peeker, audit)
                if detail is None:
                    return self._json(404, {"error": "no such item"})
                return self._json(200, detail)
            return self._json(404, {"error": "not found"})

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/healthz":
                return self._send(200, "ok", "text/plain; charset=utf-8")
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/api/whoami":
                try:
                    return self._json(200, self._whoami(self._resolve_user()))
                except _LoginBlocked as exc:
                    return self._rate_limited(exc.wait)
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
                if not self._admin_ok(user):
                    return self._json(403, {"error": "admins only"})
                return self._json(200, accounts.list())
            if url.path in ("/api/admin/audit", "/api/admin/activity", "/api/admin/reviewers", "/api/admin/items") \
                    or url.path.startswith(("/api/admin/reviewers/", "/api/admin/items/")):
                if not self._admin_ok(user):
                    return self._json(403, {"error": "admins only"})
                try:
                    limit, offset = max(0, int(qs.get("limit", 50))), max(0, int(qs.get("offset", 0)))
                except ValueError:
                    limit, offset = 50, 0
                return self._admin(url, qs, user, limit, offset)
            if url.path == "/api/me/stats":
                if not reviewer:
                    return self._json(400, {"error": "reviewer name required"})
                return self._json(200, _me_stats(store, accounts, reviewer))
            if url.path == "/api/export":
                if not self._admin_ok(user):
                    return self._json(403, {"error": "admins only"})
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
                    sessions.update_fingerprint(user.get("_token"), accounts.password_fingerprint(user["name"]))
                    sessions.revoke_user(user["name"], except_token=user.get("_token"))
                    audit.record(user["name"], "password_change", user["name"])
                    return self._json(200, {"ok": True})
                if user.get("must_change"):
                    return self._json(403, {"error": "choose a new password first", "must_change": True})
                if url.path in ("/api/users", "/api/admin/users"):
                    if not self._admin_ok(user):
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
    peeks = Peeks(state_dir / "peeks.json" if state_dir else None)
    store.peeks = peeks
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
