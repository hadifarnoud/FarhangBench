"""A local web page for human validation of benchmark items: ``python -m farhang review``.

Each reviewer answers an item blind, then sees the answer key and judges it. The server never sends an item's
key before that reviewer's blind answer is recorded, and the first blind answer cannot be changed. Reviews are
appended to ``validation/<reviewer>.jsonl`` (see ``farhang.validation``), which are plain files to commit.

Locally, reviewers type their name. For a shared deployment (``deploy/``), pass ``--users`` (a file written with
``python -m farhang review-user``): every request then needs HTTP Basic auth, and the signed-in account is the
reviewer, so nobody can answer or peek at keys under someone else's name.

The default queue is native items (the ones the FarhangScore is built from); any imported or derived source can be
reviewed too, as an audit.
"""

import io
import json
import mimetypes
import threading
import webbrowser
import zipfile
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .data import ROOT, load_items
from .runner import gold_summary
from .taxonomy import NATIVE_SOURCE, TASKS, TRACKS
from .validation import (FLAGS, REVIEWS_DIR, VERDICTS, append_record, blind_correct, content_hash, item_status,
                         load_records, merge_reviews, reviewer_slug)

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
        items, merged = self.items(), self.merged()
        out = []
        for it in items.values():
            reviews = merged.get((it["id"], content_hash(it)), {})
            mine = reviews.get(reviewer, {})
            if it["metadata"].get("author") and reviewer_slug(it["metadata"]["author"]) == reviewer:
                continue  # authors don't validate their own items
            st = item_status(it, reviews)
            out.append(dict(blind_view(it), status=st["status"], verdicts=st["verdicts"],
                            answered="blind_answer" in mine, my_verdict=mine.get("verdict")))
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
            if st["status"] != "retired":
                rows[group]["items"] += 1
            rows[group][st["status"]] += 1
            for r in reviews.values():
                if r.get("verdict"):
                    reviewers[r["reviewer"]] += 1
                if r.get("blind_correct") is not None:
                    accuracy[group].append(r["blind_correct"])
            if st["status"] == "disputed" or (st["flags"] and st["status"] != "retired"):
                problems.append({"id": it["id"], "task": it["task"], "source": source_of(it), "status": st["status"],
                                 "flags": st["flags"], "prompt": it["prompt"][:160],
                                 "comments": [{"reviewer": r["reviewer"], "verdict": r.get("verdict"),
                                               "comment": r.get("comment", ""),
                                               "suggested_answer": r.get("suggested_answer", "")}
                                              for r in reviews.values() if r.get("verdict")]})
        table = [{"group": g, **dict(c), "human_accuracy": (sum(accuracy[g]) / len(accuracy[g])) if accuracy[g] else None,
                  "blind_answers": len(accuracy[g])} for g, c in rows.items()]
        order = list(TASKS)
        table.sort(key=lambda r: (r["group"].startswith("source:"), order.index(r["group"]) if r["group"] in order else 0,
                                  r["group"]))
        return {"rows": table, "reviewers": dict(reviewers.most_common()), "problems": problems}


def summary_lines(store):
    """Plain-text progress, for ``python -m farhang review --summary``."""
    p = store.progress()
    cols = ("items", "unreviewed", "in_review", "validated", "disputed")
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


def make_handler(store, users=None):
    """Request handler; with ``users`` (a ``farhang.auth.Users``) every route but /healthz needs a login."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _user(self):
            """The signed-in reviewer, or None after sending a 401. Without accounts, returns ''."""
            if users is None:
                return ""
            name = users.check(self.headers.get("Authorization"))
            if name is None:
                body = b'{"error": "sign in required"}'
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="FarhangBench review", charset="UTF-8"')
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return name

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False))

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/healthz":
                return self._send(200, "ok", "text/plain; charset=utf-8")
            user = self._user()
            if user is None:
                return
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            reviewer = user or reviewer_slug(qs.get("reviewer", ""))
            if url.path == "/api/whoami":
                return self._json(200, {"reviewer": user or None, "signed_in": users is not None})
            if url.path == "/api/export":
                self.send_response(200)
                data = export_zip(store.reviews_dir)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", 'attachment; filename="farhangbench-reviews.zip"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            if url.path == "/":
                return self._send(200, PAGE.replace("__META__", json.dumps(
                    {"tracks": {k: v[2] for k, v in TRACKS.items()}, "tasks": TASKS, "flags": FLAGS},
                    ensure_ascii=False)), "text/html; charset=utf-8")
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
            user = self._user()
            if user is None:
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
                reviewer = user or reviewer_slug(body.get("reviewer", ""))
                if not reviewer:
                    raise ValueError("reviewer name required")
                if self.path == "/api/blind":
                    return self._json(200, store.record_blind(reviewer, body["item_id"], body.get("answer")))
                if self.path == "/api/verdict":
                    return self._json(200, store.record_verdict(
                        reviewer, body["item_id"], body.get("verdict"), body.get("flags", []),
                        body.get("comment", ""), body.get("suggested_answer", "")))
                return self._json(404, {"error": "not found"})
            except PermissionError as exc:
                return self._json(403, {"error": str(exc)})
            except (ValueError, KeyError, TypeError) as exc:
                return self._json(400, {"error": f"bad request: {exc}"})

    return Handler


def serve(data_dir, reviews_dir=REVIEWS_DIR, host="127.0.0.1", port=8766, open_browser=True, users_file=None):
    from .auth import Users
    store = ReviewStore(data_dir, reviews_dir)
    users = Users(users_file) if users_file else None
    if users is not None:
        try:
            count = len(users._load())
        except OSError as exc:
            raise SystemExit(f"cannot read the users file {users_file}: {exc}")
        if not count:
            raise SystemExit(f"the users file {users_file} has no accounts; add some with `farhang review-user`")
    if users is None and host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: serving beyond localhost without --users; anyone who can reach it can review as anyone")
    server = ThreadingHTTPServer((host, port), make_handler(store, users))
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"FarhangBench review at {url} — reviews are saved to {reviews_dir}"
          f"{' — sign-in required' if users else ''} (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FarhangBench Review</title>
<style>
:root {
  color-scheme: light;
  --surface: #fcfcfb; --surface-2: #f3f2ef; --border: #e2e1dc;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #7a7974;
  --accent: #2a78d6; --accent-soft: #e4effb; --good: #008300; --good-soft: #e3f3e3;
  --bad: #c73a39; --bad-soft: #fbe9e8; --warn: #9a6700; --warn-soft: #fbf1dc;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --surface: #1a1a19; --surface-2: #232321; --border: #34332f;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #8d8c84;
    --accent: #3987e5; --accent-soft: #1f2d3f; --good: #2fa52f; --good-soft: #1d2e1d;
    --bad: #e66767; --bad-soft: #3a2222; --warn: #d9a33a; --warn-soft: #3a3020;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface: #1a1a19; --surface-2: #232321; --border: #34332f;
  --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #8d8c84;
  --accent: #3987e5; --accent-soft: #1f2d3f; --good: #2fa52f; --good-soft: #1d2e1d;
  --bad: #e66767; --bad-soft: #3a2222; --warn: #d9a33a; --warn-soft: #3a3020;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin: 0; background: var(--surface); color: var(--text-primary); display: flex; flex-direction: column;
  font: 14px/1.55 system-ui, -apple-system, "Segoe UI", Vazirmatn, Tahoma, sans-serif; }
header { padding: 10px 16px; border-bottom: 1px solid var(--border); display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; }
header h1 { font-size: 16px; margin: 0; } header h1 span { color: var(--text-muted); font-weight: 400; }
.tabs { display: flex; gap: 4px; } .tabs button.on { background: var(--accent-soft); border-color: var(--accent); }
.controls { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
input, select, textarea, button { font: inherit; color: var(--text-primary); background: var(--surface-2);
  border: 1px solid var(--border); border-radius: 6px; padding: 5px 9px; }
button { cursor: pointer; } button:disabled { opacity: .5; cursor: default; }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.muted { color: var(--text-muted); } .fa { unicode-bidi: plaintext; }
.layout { flex: 1; display: grid; grid-template-columns: minmax(260px, 30%) 1fr; min-height: 0; }
#list { overflow: auto; border-right: 1px solid var(--border); }
#list .row { padding: 7px 12px; border-bottom: 1px solid var(--border); cursor: pointer; }
#list .row:hover { background: var(--surface-2); } #list .row.sel { background: var(--accent-soft); }
.row .top { display: flex; gap: 6px; align-items: baseline; font-size: 12px; color: var(--text-muted); }
.row .top code { color: var(--text-primary); }
.row .txt { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; unicode-bidi: plaintext; }
.chip { display: inline-block; padding: 0 7px; border-radius: 10px; font-size: 11.5px; border: 1px solid var(--border);
  background: var(--surface-2); color: var(--text-secondary); white-space: nowrap; }
.chip.validated { background: var(--good-soft); color: var(--good); border-color: transparent; }
.chip.disputed { background: var(--bad-soft); color: var(--bad); border-color: transparent; }
.chip.in_review { background: var(--warn-soft); color: var(--warn); border-color: transparent; }
.chip.mine { background: var(--accent-soft); color: var(--accent); border-color: transparent; }
#main { overflow: auto; padding: 18px 24px 60px; }
.card { max-width: 820px; }
.card h2 { font-size: 15px; margin: 0 0 2px; } h3 { font-size: 13px; color: var(--text-secondary); margin: 18px 0 6px; font-weight: 600; }
.block { background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px;
  white-space: pre-wrap; unicode-bidi: plaintext; font-size: 16px; line-height: 1.8; }
.opts { display: grid; gap: 6px; }
.opt { text-align: start; padding: 9px 12px; border-radius: 8px; font-size: 15.5px; unicode-bidi: plaintext; display: flex; gap: 10px; align-items: baseline; }
.opt b { color: var(--text-muted); font-size: 12px; min-width: 14px; }
.opt.sel { border-color: var(--accent); background: var(--accent-soft); }
.opt.key { border-color: var(--good); background: var(--good-soft); }
.opt.wrongpick { border-color: var(--bad); background: var(--bad-soft); }
.opt .tag { margin-inline-start: auto; font-size: 11.5px; color: var(--text-muted); }
.images img { max-width: 100%; max-height: 380px; border-radius: 6px; border: 1px solid var(--border); display: block; }
.images figcaption { font-size: 12px; color: var(--text-muted); margin: 4px 0 8px; }
.answer-input { width: 100%; font-size: 16px; unicode-bidi: plaintext; }
textarea.answer-input { min-height: 120px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 14px; }
.hint { font-size: 12.5px; color: var(--text-muted); }
.result { margin: 10px 0; padding: 8px 12px; border-radius: 8px; }
.result.ok { background: var(--good-soft); color: var(--good); } .result.no { background: var(--bad-soft); color: var(--bad); }
.result.neutral { background: var(--surface-2); color: var(--text-secondary); }
.verdicts { display: flex; gap: 8px; flex-wrap: wrap; }
.verdicts button { padding: 9px 14px; border-radius: 8px; }
.verdicts button.on[data-v="correct"] { background: var(--good-soft); border-color: var(--good); color: var(--good); }
.verdicts button.on[data-v="wrong"] { background: var(--bad-soft); border-color: var(--bad); color: var(--bad); }
.verdicts button.on[data-v="unsure"] { background: var(--warn-soft); border-color: var(--warn); color: var(--warn); }
.flags { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 4px 12px; }
.flags label { display: flex; gap: 6px; align-items: center; }
.field { width: 100%; unicode-bidi: plaintext; } textarea.field { min-height: 70px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
table.kv td:first-child { color: var(--text-muted); white-space: nowrap; width: 1%; }
.accept span { display: inline-block; margin: 0 4px 4px 0; padding: 1px 8px; border-radius: 10px; background: var(--good-soft); unicode-bidi: plaintext; }
.empty { color: var(--text-muted); padding: 50px 0; text-align: center; }
.msg { margin: 10px 0; padding: 8px 10px; border-radius: 6px; border: 1px solid var(--bad); color: var(--bad); }
.welcome { max-width: 620px; } .welcome li { margin-bottom: 6px; }
@media (max-width: 760px) { .layout { grid-template-columns: 1fr; } #list { max-height: 35vh; border-right: 0; border-bottom: 1px solid var(--border); } #main { padding: 14px 16px 40px; } }
</style>
</head>
<body>
<header>
  <h1>FarhangBench review <span>· بازبینی</span></h1>
  <div class="tabs"><button id="t-review" class="on">Review</button><button id="t-progress">Progress</button></div>
  <label class="controls">Your name <input id="who" placeholder="e.g. maryam" size="14" autocomplete="name"></label>
  <div class="controls" id="filters">
    <select id="f-scope">
      <option value="native">Native items (FarhangScore)</option>
      <option value="reference">Imported and derived (audit)</option>
      <option value="all">All items</option>
    </select>
    <select id="f-track"><option value="">All tracks</option></select>
    <select id="f-task"><option value="">All tasks</option></select>
    <select id="f-show">
      <option value="todo">Not reviewed by me</option>
      <option value="mine">Reviewed by me</option>
      <option value="disputed">Disputed</option>
      <option value="all">Everything</option>
    </select>
    <span id="count" class="muted"></span>
  </div>
</header>
<div class="layout" id="review-view">
  <div id="list"></div>
  <div id="main"></div>
</div>
<div id="progress-view" style="display:none; overflow:auto; padding: 18px 24px 60px; flex: 1"></div>
<script>
const META = __META__;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const STATUS = { unreviewed: "Unreviewed", in_review: "In review", validated: "Validated", disputed: "Disputed", retired: "Retired" };
let ITEMS = [], current = null, choice = null, verdict = null;
const store = (k, v) => { try { v === undefined ? null : localStorage.setItem(k, v); return localStorage.getItem(k); } catch (e) { return null; } };
$("who").value = store("farhang-reviewer") || "";
let SIGNED_IN = false;

async function api(path, body) {
  const res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const data = await res.json().catch(() => ({ error: "bad response" }));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
const who = () => $("who").value.trim();

function fill(id, pairs, label) {
  const el = $(id), cur = el.value;
  el.innerHTML = `<option value="">${label}</option>` + pairs.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("");
  el.value = cur;
}
function initFilters() {
  fill("f-track", Object.entries(META.tracks).map(([k, v]) => [k, `${k} — ${v}`]), "All tracks");
  fill("f-task", Object.keys(META.tasks).filter(t => !$("f-track").value || t[0] === $("f-track").value).map(t => [t, t]), "All tasks");
}
function filtered() {
  const scope = $("f-scope").value, show = $("f-show").value;
  return ITEMS.filter(it => (scope === "all" || (scope === "native") === (it.source === "farhangbench"))
    && it.status !== "retired"
    && (!$("f-track").value || it.track === $("f-track").value) && (!$("f-task").value || it.task === $("f-task").value)
    && (show === "all" || (show === "todo" && !it.my_verdict) || (show === "mine" && it.my_verdict) || (show === "disputed" && it.status === "disputed")));
}
function renderList() {
  const rows = filtered();
  $("count").textContent = `${rows.length.toLocaleString()} items`;
  $("list").innerHTML = rows.slice(0, 400).map(it => `<div class="row${current && current.id === it.id ? " sel" : ""}" data-id="${esc(it.id)}">
    <div class="top"><code>${esc(it.id)}</code><span>${esc(it.task)}</span>
    <span class="chip ${it.status}">${STATUS[it.status]}${it.verdicts ? " · " + it.verdicts : ""}</span>
    ${it.my_verdict ? `<span class="chip mine">mine: ${esc(it.my_verdict)}</span>` : it.answered ? '<span class="chip mine">answered</span>' : ""}</div>
    <div class="txt">${esc(it.prompt)}</div></div>`).join("")
    + (rows.length > 400 ? `<div class="empty">Showing 400 of ${rows.length}; narrow the filters.</div>` : "")
    + (rows.length ? "" : `<div class="empty">Nothing left in this view. ${$("f-show").value === "todo" ? "Well done!" : ""}</div>`);
}
$("list").addEventListener("click", e => { const row = e.target.closest(".row"); if (row) open(row.dataset.id); });

function welcome() {
  $("main").innerHTML = `<div class="welcome"><h2>How reviewing works</h2><ol>
    <li><b>Answer first, without the key.</b> Pick or type the answer you believe is right, or press “I don't know”. Your first answer is saved and can't be changed. This is what makes the review independent.</li>
    <li><b>Then judge the key.</b> You'll see the official answer. Mark it correct, wrong or not sure, tick any problems, and leave a note or the right answer if you know it.</li>
    <li>An item counts towards the FarhangScore once two reviewers, other than its author, have answered and at least two thirds of them say the key is correct.</li>
  </ol><p class="muted">Use reliable sources (dictionaries, Ganjoor, Dehkhoda, Farhangestan) when you check a key. Don't ask an AI model: the point is a human check.</p>
  <p class="muted">Keys: 1–5 pick an option · Enter submit · after the key: C correct, W wrong, U not sure · Ctrl+Enter save and go to the next item.</p>
  ${who() ? `<button class="primary" id="go">Start with the first item</button>` : `<p><b>Type your name at the top to begin.</b></p>`}</div>`;
  if ($("go")) $("go").onclick = next;
}
function next() {
  const rows = filtered().filter(it => !it.my_verdict && (!current || it.id !== current.id));
  rows.length ? open(rows[0].id) : ($("main").innerHTML = '<div class="empty">No more items to review in this view.</div>');
}
const imgs = it => (it.images || []).length ? `<div class="images">` + it.images.map(im =>
  `<figure style="margin:0 0 6px"><img src="/${esc(im.path)}" alt="Item image"><figcaption>${esc(im.credit || "")} · ${esc(im.license || "")}</figcaption></figure>`).join("") + "</div>" : "";

async function open(id) {
  current = ITEMS.find(it => it.id === id); choice = null; verdict = null;
  renderList();
  if (current.answered) {
    try { return renderKey(await api(`/api/key?reviewer=${encodeURIComponent(who())}&id=${encodeURIComponent(id)}`)); }
    catch (e) { /* fall through to blind */ }
  }
  renderBlind();
}
function head(it) {
  return `<h2><code>${esc(it.id)}</code></h2><div class="muted">${esc(META.tracks[it.track] || it.track)} · ${esc(it.task)} — ${esc(META.tasks[it.task] || "")}${it.source !== "farhangbench" ? ` · source: ${esc(it.source)}` : ""}</div>`
    + imgs(it) + (it.context ? `<h3>Context · متن</h3><div class="block" dir="auto">${esc(it.context)}</div>` : "")
    + `<h3>Question · پرسش</h3><div class="block" dir="auto">${esc(it.prompt)}</div>`;
}
function renderBlind() {
  const it = current;
  let h = `<div class="card">${head(it)}<h3>Your answer · پاسخ شما <span class="muted" style="font-weight:400">(before you see the key)</span></h3>`;
  if (it.format === "mcq") h += `<div class="opts">` + it.options.map((o, i) => `<button class="opt" data-i="${i}" dir="auto"><b>${i + 1}</b><span>${esc(o)}</span></button>`).join("") + "</div>";
  else if (it.format === "judgment") h += `<div class="opts">` + it.labels.map((l, i) => `<button class="opt" data-i="${i}" dir="auto"><b>${i + 1}</b><span>${esc(l)}</span></button>`).join("") + "</div>";
  else if (it.format === "generation") h += `<textarea class="answer-input" id="ans" dir="auto" placeholder="Optional: write your own answer. Generation items are scored by a rubric, so only your verdict on the rubric counts."></textarea>`;
  else h += `<input class="answer-input" id="ans" dir="auto" placeholder="Type your answer">`;
  h += `<div class="actions"><button class="primary" id="submit">Submit answer · ثبت پاسخ</button><button id="idk">I don't know · نمی‌دانم</button>
    <span class="hint">Your first answer is saved and can't be changed.</span></div><div id="err"></div></div>`;
  $("main").innerHTML = h; $("main").scrollTop = 0;
  document.querySelectorAll(".opt").forEach(b => b.onclick = () => pick(+b.dataset.i));
  $("submit").onclick = () => submitBlind(false);
  $("idk").onclick = () => submitBlind(true);
  if ($("ans")) $("ans").focus();
}
function pick(i) { choice = i; document.querySelectorAll(".opt").forEach(b => b.classList.toggle("sel", +b.dataset.i === i)); }
async function submitBlind(dontKnow) {
  if (!who()) return showErr("Type your name at the top first.");
  const it = current;
  let answer = null;
  if (!dontKnow) {
    if (it.format === "mcq") answer = choice;
    else if (it.format === "judgment") answer = choice === null ? null : it.labels[choice];
    else answer = $("ans").value.trim();
    if (answer === null || answer === "") { if (it.format !== "generation") return showErr("Pick or type an answer, or press “I don't know”."); }
  }
  try {
    const key = await api("/api/blind", { reviewer: who(), item_id: it.id, answer });
    it.answered = true; renderKey(key);
  } catch (e) { showErr(e.message); }
}
function showErr(text) { const box = $("err"); if (box) box.innerHTML = `<div class="msg">${esc(text)}</div>`; }

function renderKey(key) {
  const it = current, rv = key.review || {}, a = key.answer || {}, md = key.metadata || {};
  verdict = rv.verdict || null;
  let h = `<div class="card">${head(it)}`;
  const mine = rv.blind_answer;
  if (it.format === "mcq" || it.format === "judgment") {
    const opts = it.format === "mcq" ? it.options : it.labels;
    const keyIdx = it.format === "mcq" ? a.index : opts.indexOf(a.label);
    const mineIdx = it.format === "mcq" ? mine : opts.indexOf(mine);
    h += `<h3>Options · گزینه‌ها</h3><div class="opts">` + opts.map((o, i) => `<div class="opt${i === keyIdx ? " key" : ""}${i === mineIdx && i !== keyIdx ? " wrongpick" : ""}" dir="auto"><b>${i + 1}</b><span>${esc(o)}</span>`
      + `<span class="tag">${i === keyIdx ? "key" : ""}${i === mineIdx ? (i === keyIdx ? " · your answer" : "your answer") : ""}${key.distractor_types ? " · " + esc(key.distractor_types[i]) : ""}</span></div>`).join("") + "</div>";
  } else {
    h += `<h3>Key · پاسخ رسمی</h3>` + (a.accept ? `<div class="accept">${a.accept.map(x => `<span dir="auto">${esc(x)}</span>`).join("")}</div>` : `<div class="muted">Scored by rubric</div>`);
    if (a.reject && a.reject.length) h += `<div class="muted" dir="auto">Rejected: ${a.reject.map(esc).join(" · ")}</div>`;
    h += `<h3>Your answer · پاسخ شما</h3><div class="block" dir="auto">${mine ? esc(mine) : '<span class="muted">(none)</span>'}</div>`;
  }
  if (rv.blind_correct === true) h += `<div class="result ok">Your blind answer matches the key.</div>`;
  else if (rv.blind_correct === false) h += `<div class="result no">${mine === null || mine === "" ? "You didn't know the answer." : "Your blind answer differs from the key. Is the key wrong, or is your answer?"}</div>`;
  else h += `<div class="result neutral">Generation item: judge whether the rubric and reference below describe a good answer.</div>`;
  if (key.rubric) h += `<h3>Rubric · معیارها</h3><table class="kv">` + key.rubric.map(c => `<tr><td>${esc(c.id)}${c.gate ? " (gate)" : ""}</td><td dir="auto">${esc(c.description)}</td></tr>`).join("") + "</table>";
  if (key.reference) h += `<h3>Reference answer</h3><div class="block" dir="auto">${esc(key.reference)}</div>`;
  const notes = ["variation_note", "sources", "as_of", "region", "difficulty"].filter(k => md[k]);
  if (notes.length) h += `<h3>Notes from the author</h3><table class="kv">` + notes.map(k => `<tr><td>${esc(k)}</td><td dir="auto">${esc(Array.isArray(md[k]) ? md[k].join(", ") : md[k])}</td></tr>`).join("") + "</table>";
  h += `<h3>Is the key correct? · آیا پاسخ رسمی درست است؟</h3><div class="verdicts">
      <button data-v="correct">Correct · درست (C)</button><button data-v="wrong">Wrong · نادرست (W)</button><button data-v="unsure">Not sure · مطمئن نیستم (U)</button></div>
    <h3>Problems with the item</h3><div class="flags">` + Object.entries(META.flags).map(([k, label]) =>
      `<label><input type="checkbox" value="${k}"${(rv.flags || []).includes(k) ? " checked" : ""}> ${esc(label)}</label>`).join("") + `</div>
    <h3>Correct answer, if the key is wrong</h3><input class="field" id="sugg" dir="auto" value="${esc(rv.suggested_answer || "")}">
    <h3>Comment and source</h3><textarea class="field" id="comment" dir="auto" placeholder="e.g. Dehkhoda gives …">${esc(rv.comment || "")}</textarea>
    <div class="actions"><button class="primary" id="save">Save and next · ذخیره</button><span class="hint">${rv.verdict ? `You saved “${esc(rv.verdict)}” earlier; saving again replaces it.` : ""}</span></div><div id="err"></div></div>`;
  $("main").innerHTML = h; $("main").scrollTop = 0;
  document.querySelectorAll(".verdicts button").forEach(b => { b.classList.toggle("on", b.dataset.v === verdict); b.onclick = () => setVerdict(b.dataset.v); });
  $("save").onclick = saveVerdict;
}
function setVerdict(v) { verdict = v; document.querySelectorAll(".verdicts button").forEach(b => b.classList.toggle("on", b.dataset.v === v)); }
async function saveVerdict() {
  if (!verdict) return showErr("Choose correct, wrong or not sure.");
  const flags = [...document.querySelectorAll(".flags input:checked")].map(x => x.value);
  try {
    await api("/api/verdict", { reviewer: who(), item_id: current.id, verdict, flags, comment: $("comment").value, suggested_answer: $("sugg").value });
    await reload(); next();
  } catch (e) { showErr(e.message); }
}
document.addEventListener("keydown", e => {
  if (!current || $("review-view").style.display === "none") return;
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && $("save")) { e.preventDefault(); return saveVerdict(); }
  if (e.key === "Enter" && $("submit") && (!typing || document.activeElement.id === "ans") && document.activeElement.tagName !== "TEXTAREA") { e.preventDefault(); return submitBlind(false); }
  if (typing) return;
  if ($("submit") && /^[1-5]$/.test(e.key) && document.querySelector(`.opt[data-i="${+e.key - 1}"]`)) pick(+e.key - 1);
  if ($("save")) { const v = { c: "correct", w: "wrong", u: "unsure" }[e.key.toLowerCase()]; if (v) setVerdict(v); }
});

async function reload() {
  if (!who()) { ITEMS = []; renderList(); return; }
  ITEMS = await api(`/api/items?reviewer=${encodeURIComponent(who())}`);
  if (current) current = ITEMS.find(it => it.id === current.id) || null;
  renderList();
}
async function renderProgress() {
  const p = await api("/api/progress");
  const pct = v => v === null || v === undefined ? "–" : (100 * v).toFixed(0) + "%";
  const row = r => `<tr><td>${esc(r.group.replace("source:", ""))}</td><td class="num">${r.items || 0}</td><td class="num">${r.unreviewed || 0}</td>
    <td class="num">${r.in_review || 0}</td><td class="num">${r.validated || 0}</td><td class="num">${r.disputed || 0}</td>
    <td class="num">${pct(r.human_accuracy)}</td><td class="num">${r.blind_answers}</td></tr>`;
  const table = (title, rows) => `<h3>${title}</h3><table><thead><tr><th></th><th class="num">Items</th><th class="num">Unreviewed</th><th class="num">In review</th>
    <th class="num">Validated</th><th class="num">Disputed</th><th class="num">Human accuracy</th><th class="num">Blind answers</th></tr></thead><tbody>${rows.map(row).join("")}</tbody></table>`;
  const native = p.rows.filter(r => !r.group.startsWith("source:")), ref = p.rows.filter(r => r.group.startsWith("source:"));
  const tot = native.reduce((a, r) => { for (const k of ["items", "validated", "disputed"]) a[k] += r[k] || 0; return a; }, { items: 0, validated: 0, disputed: 0 });
  $("progress-view").innerHTML = `<div style="max-width:1000px"><h2 style="font-size:17px">Progress</h2>
    <p>${tot.validated} of ${tot.items} native items validated${tot.disputed ? `, ${tot.disputed} disputed` : ""}. Human accuracy is the share of blind answers that matched the key: a human baseline, and a warning sign when it is low.</p>
    ${table("Native items by task", native)}${ref.some(r => r.blind_answers) ? table("Imported and derived sources (audit)", ref.filter(r => r.blind_answers)) : ""}
    <h3>Reviewers</h3><p>${Object.entries(p.reviewers).map(([k, v]) => `${esc(k)} (${v})`).join(", ") || "No reviews yet."}
    · <a href="/api/export">Download all reviews (zip)</a> to commit them to <code>validation/</code></p>
    <h3>Disputed or flagged items, for authors to fix</h3>` + (p.problems.length ? `<table><thead><tr><th>Item</th><th>Status and flags</th><th>Reviewer notes</th></tr></thead><tbody>`
      + p.problems.map(x => `<tr><td><code>${esc(x.id)}</code><div class="muted">${esc(x.task)}</div><div class="fa" dir="auto">${esc(x.prompt)}</div></td>
        <td><span class="chip ${x.status}">${STATUS[x.status]}</span><div class="muted">${x.flags.map(f => esc(META.flags[f] || f)).join(", ")}</div></td>
        <td>${x.comments.map(c => `<div><b>${esc(c.reviewer)}</b>: ${esc(c.verdict)}${c.suggested_answer ? ` — suggests <span dir="auto">${esc(c.suggested_answer)}</span>` : ""}${c.comment ? `<div class="fa" dir="auto">${esc(c.comment)}</div>` : ""}</div>`).join("")}</td></tr>`).join("")
      + "</tbody></table>" : `<p class="muted">None.</p>`) + `<p class="muted">Fix items in <code>python -m farhang browse</code>. Editing an item resets its reviews, because reviewers judged the old version.</p></div>`;
}
function tab(name) {
  $("t-review").classList.toggle("on", name === "review"); $("t-progress").classList.toggle("on", name === "progress");
  $("review-view").style.display = name === "review" ? "" : "none"; $("filters").style.display = name === "review" ? "" : "none";
  $("progress-view").style.display = name === "progress" ? "block" : "none";
  if (name === "progress") renderProgress().catch(e => $("progress-view").innerHTML = `<div class="msg">${esc(e.message)}</div>`);
}
$("t-review").onclick = () => tab("review"); $("t-progress").onclick = () => tab("progress");
$("who").addEventListener("change", async () => { store("farhang-reviewer", who()); current = null; await reload(); welcome(); });
["f-scope", "f-task", "f-show"].forEach(id => $(id).addEventListener("input", renderList));
$("f-track").addEventListener("input", () => { $("f-task").value = ""; initFilters(); renderList(); });
(async () => {
  try {
    const me = await api("/api/whoami");
    if (me.signed_in) { SIGNED_IN = true; $("who").value = me.reviewer; $("who").readOnly = true; $("who").title = "Signed in"; }
  } catch (e) { /* older server */ }
  initFilters(); welcome(); reload();
})();
</script>
</body>
</html>
"""
