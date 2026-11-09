"""End-to-end check of the reviewer app (``python -m farhang review``): ``python3 tests/e2e_review.py``.

Starts the real server on a free port with throwaway reviews/state directories and a bootstrap admin, against the
real ``data/``, then:

1. drives the HTTP API with the standard library: failed sign-in, the rate limit, admin sign-in (cookie), creating
   a reviewer with a one-time password, the reviewer's forced password change, a blind answer then a verdict, the
   admin activity feed and reviewer stats, the admin item audit (the peek is recorded and the admin's own blind
   answer is then refused), disabling a reviewer (their session dies) and signing out;
2. clicks through the real page in Chromium (``tests/e2e_browser.mjs``, Playwright from Node) and screenshots every
   screen at 1280 px and 390 px wide, light and dark, failing on any console error or CSP violation. A second
   server without accounts checks local mode.

Artifacts go to ``artifacts/e2e-review/<timestamp>/``: ``report.json`` (every step, pass or fail) plus the PNGs.

Playwright isn't a dependency of the project. Install it once anywhere outside the repo and point
``FARHANG_E2E_PLAYWRIGHT`` at that directory (browsers come from ``~/.cache/ms-playwright``)::

    mkdir -p ~/.cache/farhang-e2e && cd ~/.cache/farhang-e2e && npm init -y && npm install --save-exact playwright@1.62.0
    npx playwright install chromium   # only if that browser build isn't cached yet

``--no-browser`` runs the HTTP part only. Exit status is 0 only when every step passed.
"""

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from farhang.auth import user_line  # noqa: E402

ADMIN, ADMIN_PW = "admin", "admin-pass-e2e-1"
REVIEWER, REVIEWER_DISPLAY = "sara", "سارا"
REVIEWER_PW = "sara-chosen-pass-42"
DEFAULT_PLAYWRIGHT = Path.home() / ".cache" / "farhang-e2e"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    """A tiny HTTP client with its own cookie, like one browser."""

    def __init__(self, base):
        self.base, self.cookie = base, None

    def call(self, path, body=None, headers=None):
        hdrs = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        if self.cookie:
            hdrs["Cookie"] = f"fb_session={self.cookie}"
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                status, raw, h = resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as exc:
            status, raw, h = exc.code, exc.read(), exc.headers
        for value in h.get_all("Set-Cookie") or []:
            first = value.split(";")[0]
            name, _, token = first.partition("=")
            if name == "fb_session":
                self.cookie = token if token and "Max-Age=0" not in value else None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = raw
        return status, payload, h


def expect(cond, message):
    if not cond:
        raise AssertionError(message)


class Run:
    def __init__(self, out):
        self.out = out
        self.steps = []

    def step(self, phase, name, fn):
        t0 = time.time()
        try:
            detail = fn()
            self.steps.append({"phase": phase, "name": name, "ok": True, "ms": int(1000 * (time.time() - t0)),
                               "detail": detail})
            print(f"  ok    {name}")
            return True
        except Exception as exc:  # noqa: BLE001 — every failure goes in the report
            self.steps.append({"phase": phase, "name": name, "ok": False, "ms": int(1000 * (time.time() - t0)),
                               "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()})
            print(f"  FAIL  {name}: {exc}")
            return False


def start_server(tmp, port, accounts=True):
    cmd = [sys.executable, "-m", "farhang", "review", "--data", str(ROOT / "data"),
           "--reviews", str(tmp / "validation"), "--host", "127.0.0.1", "--port", str(port), "--no-open"]
    if accounts:
        cmd += ["--users", str(tmp / "users"), "--accounts", str(tmp / "state" / "accounts.json")]
    env = dict(os.environ, FARHANG_USERS_FILE="", FARHANG_ACCOUNTS_FILE="", FARHANG_SECURE_COOKIES="")
    log = open(tmp / f"server-{port}.log", "w")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=env)
    base = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=2) as resp:
                if resp.status == 200:
                    return proc, base
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise SystemExit(f"server didn't start; see {tmp / f'server-{port}.log'}")


def http_phase(run, base, ctx):
    admin, sara, anon = Client(base), Client(base), Client(base)
    P = "http"

    def unauthenticated():
        status, who, _ = anon.call("/api/whoami")
        expect(status == 200 and who["auth_enabled"] and not who["signed_in"], f"whoami: {who}")
        status, body, headers = anon.call("/api/items")
        expect(status == 401 and body.get("error") == "sign in required", f"items without sign-in: {status} {body}")
        expect("WWW-Authenticate" not in headers, "401 must not send WWW-Authenticate (browser popup)")
        status, page, headers = anon.call("/")
        expect(status == 200 and "default-src 'self'" in headers.get("Content-Security-Policy", ""), "CSP header on /")
        for name in ("app.js", "app.css"):
            expect(anon.call(f"/static/{name}")[0] == 200, f"/static/{name}")
        expect(anon.call("/static/../review.py")[0] == 404, "static whitelist")
        return "401 JSON, no WWW-Authenticate, CSP on the page"

    def failed_login():
        status, body, _ = anon.call("/api/login", {"name": ADMIN, "password": "wrong-password"})
        expect(status == 401 and body["error"] == "wrong name or password", f"{status} {body}")
        status, body, _ = anon.call("/api/login", {"name": "nobody-here", "password": "x"})
        expect(status == 401 and body["error"] == "wrong name or password", "same message for unknown names")
        expect(anon.cookie is None, "no cookie after a failed sign-in")

    def rate_limit():
        for i in range(5):
            status, _, _ = anon.call("/api/login", {"name": "ghost", "password": f"nope-{i}"})
            expect(status == 401, f"try {i}: {status}")
        status, body, _ = anon.call("/api/login", {"name": "ghost", "password": "nope-6"})
        expect(status == 429 and body.get("retry_after", 0) > 0, f"6th try: {status} {body}")
        return f"429 after 5 failures, retry_after={body['retry_after']}s"

    def admin_login():
        status, body, headers = admin.call("/api/login", {"name": ADMIN, "password": ADMIN_PW})
        expect(status == 200 and body["user"]["role"] == "admin", f"{status} {body}")
        cookie = headers.get("Set-Cookie", "")
        expect(admin.cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie, f"cookie: {cookie}")
        status, who, _ = admin.call("/api/whoami")
        expect(who["signed_in"] and who["reviewer"] == ADMIN, f"whoami: {who}")

    def basic_auth_rate_limited():
        bad = {"Authorization": "Basic " + base64.b64encode(b"basic-bot:wrong-pass").decode()}
        for i in range(5):
            status, body, headers = anon.call("/api/items", headers=bad)
            expect(status == 401 and "WWW-Authenticate" not in headers, f"try {i}: {status}")
        status, body, headers = anon.call("/api/items", headers=bad)
        expect(status == 429 and body.get("retry_after", 0) > 0 and "WWW-Authenticate" not in headers,
               f"6th try: {status} {body}")
        log = admin.call("/api/admin/audit?action=login_failed&actor=basic-bot")[1]
        expect(log["total"] >= 1, f"Basic failures audited: {log}")
        return "5x 401 then 429 JSON for Basic auth, login_failed audited, no WWW-Authenticate"

    def create_reviewer():
        status, body, _ = admin.call("/api/admin/users", {"action": "add", "name": REVIEWER, "role": "reviewer",
                                                          "display_name": REVIEWER_DISPLAY})
        expect(status == 200 and body["name"] == REVIEWER and len(body["password"] or "") >= 10, f"{status} {body}")
        ctx["otp"] = body["password"]
        users = {u["name"]: u for u in admin.call("/api/admin/users")[1]}
        expect(users[REVIEWER]["must_change"] and users[REVIEWER]["display_name"] == REVIEWER_DISPLAY, users)

    def reviewer_must_change():
        status, body, _ = sara.call("/api/login", {"name": REVIEWER, "password": ctx["otp"]})
        expect(status == 200 and body["user"]["must_change"], f"{status} {body}")
        status, body, _ = sara.call("/api/items")
        expect(status == 403 and body.get("must_change"), f"items before choosing a password: {status} {body}")
        status, body, _ = sara.call("/api/password", {"new_password": "short"})
        expect(status == 400, "short password refused")
        status, body, _ = sara.call("/api/password", {"new_password": REVIEWER_PW})
        expect(status == 200, f"{status} {body}")
        expect(not sara.call("/api/whoami")[1]["must_change"], "must_change cleared")

    def review_flow():
        status, items, _ = sara.call("/api/items")
        expect(status == 200 and items, "listing")
        native = [i for i in items if i["source"] == "farhangbench" and i["format"] == "mcq" and not i["answered"]]
        expect(len(native) >= 4, "need native mcq items")
        item, ctx["peek_item"], ctx["browser_item"] = native[0], native[1]["id"], native[2]["id"]
        ctx["reviewed_item"] = item["id"]
        status, body, _ = sara.call(f"/api/key?id={item['id']}")
        expect(status == 403, "no key before the blind answer")
        status, key, _ = sara.call("/api/blind", {"item_id": item["id"], "answer": 0})
        expect(status == 200 and "answer" in key and key["review"]["blind_answer"] == 0, f"{status} {key}")
        status, _, _ = sara.call("/api/blind", {"item_id": item["id"], "answer": 1})
        first = sara.call(f"/api/key?id={item['id']}")[1]["review"]["blind_answer"]
        expect(first == 0, "the first blind answer stands")
        status, body, _ = sara.call("/api/verdict", {"item_id": item["id"], "verdict": "wrong", "flags": ["ambiguous"],
                                                     "comment": "دهخدا معنای دیگری می‌دهد",
                                                     "suggested_answer": "گزینهٔ دوم"})
        expect(status == 200 and body["review"]["verdict"] == "wrong", f"{status} {body}")
        # A second review, left for the browser's item audit (the admin peeks the first one over HTTP below).
        ctx["ui_audit_item"] = native[3]["id"]
        expect(sara.call("/api/blind", {"item_id": native[3]["id"], "answer": 1})[0] == 200, "second blind answer")
        expect(sara.call("/api/verdict", {"item_id": native[3]["id"], "verdict": "wrong", "flags": ["outdated"],
                                          "comment": "امروزه کمتر شنیده می‌شود"})[0] == 200, "second verdict")
        mine = next(i for i in sara.call("/api/items")[1] if i["id"] == item["id"])
        expect(mine["my_verdict"] == "wrong" and mine["my_flags"] == ["ambiguous"] and mine["my_time"], mine)
        return f"blind → key → verdict on {item['id']}"

    def admin_activity():
        status, body, _ = admin.call(f"/api/admin/activity?reviewer={REVIEWER}")
        expect(status == 200 and body["total"] >= 2, f"{status} {body}")
        types = [r["type"] for r in body["rows"]]
        expect(types[0] == "verdict" and "blind" in types, f"newest first: {types}")
        expect(body["rows"][0]["comment"] and body["rows"][0]["current"], body["rows"][0])
        status, body, _ = sara.call("/api/admin/activity")
        expect(status == 403, "reviewers can't see the admin feed")

    def reviewer_stats():
        status, me, _ = sara.call("/api/me/stats")
        expect(status == 200 and me["blind_answers"] == 2 and me["verdicts"] == 2, me)
        expect(me["queue"]["native"] > 0 and len(me["per_day"]) == 14, me)
        status, row, _ = admin.call(f"/api/admin/reviewers/{REVIEWER}")
        expect(status == 200 and row["verdict_counts"]["wrong"] == 2 and row["recent"], row)
        expect(row["display_name"] == REVIEWER_DISPLAY, row)

    def activity_hides_answers_until_peek():
        item = ctx["reviewed_item"]
        rows = [r for r in admin.call(f"/api/admin/activity?item={item}")[1]["rows"]
                if r["item_id"] == item and r["current"]]
        expect(rows and all(r.get("hidden") for r in rows), f"current rows hidden for the admin: {rows}")
        expect(not any(k in r for r in rows for k in ("answer", "correct", "comment", "suggested_answer")), rows)
        rrow = admin.call(f"/api/admin/reviewers/{REVIEWER}")[1]
        expect(all(r.get("hidden") for r in rrow["recent"] if r["item_id"] == item and r["current"]),
               "the reviewer page hides them too")
        expect(admin.call(f"/api/admin/items/{item}")[0] == 200, "open the audit view (recorded peek)")
        rows = [r for r in admin.call(f"/api/admin/activity?item={item}")[1]["rows"]
                if r["item_id"] == item and r["current"]]
        expect(rows and all(not r.get("hidden") for r in rows) and any("answer" in r for r in rows),
               f"visible after the peek: {rows}")
        return f"other reviewers' answers hidden on {item} until the recorded peek"

    def admin_peek():
        item = ctx["peek_item"]
        rows = admin.call(f"/api/admin/items?q={item}")[1]["rows"]
        row = next(r for r in rows if r["id"] == item)
        expect(not row["my_answered"] and not row["peeked"], row)
        status, detail, _ = admin.call(f"/api/admin/items/{item}")
        expect(status == 200 and detail["peeked"] and "answer" in detail["item"], f"{status}")
        audit = admin.call("/api/admin/audit?action=peek")[1]
        expect(audit["total"] >= 1 and audit["rows"][0]["target"].startswith(item + "@"), audit)
        status, body, _ = admin.call("/api/blind", {"item_id": item, "answer": 0})
        expect(status == 403 and "blind" in body["error"], f"admin blind answer after a peek: {status} {body}")
        listed = next(i for i in admin.call("/api/items")[1] if i["id"] == item)
        expect(listed["peeked"], "listing marks the peeked item")
        detail = admin.call(f"/api/admin/items/{ctx['reviewed_item']}")[1]
        expect(any(r["reviewer"] == REVIEWER and r["verdict"] == "wrong" for r in detail["reviews"]), detail["reviews"])
        return f"peek on {item} recorded and audited; admin's blind answer refused"

    def disable_kills_session():
        status, _, _ = admin.call("/api/admin/users", {"action": "disable", "name": REVIEWER})
        expect(status == 200, "disable")
        status, body, _ = sara.call("/api/items")
        expect(status == 401, f"disabled reviewer's cookie still works: {status}")
        status, body, _ = Client(base).call("/api/login", {"name": REVIEWER, "password": REVIEWER_PW})
        expect(status == 401, "disabled reviewer can't sign in")
        status, _, _ = admin.call("/api/admin/users", {"action": "enable", "name": REVIEWER})
        expect(status == 200, "enable")
        status, _, _ = sara.call("/api/login", {"name": REVIEWER, "password": REVIEWER_PW})
        expect(status == 200, "signs in again once enabled")

    def logout():
        status, _, headers = admin.call("/api/logout", {})
        expect(status == 200 and "Max-Age=0" in headers.get("Set-Cookie", ""), "cookie cleared")
        expect(admin.cookie is None, "client dropped the cookie")
        status, who, _ = admin.call("/api/whoami")
        expect(not who["signed_in"], who)

    def csrf():
        status, _, _ = sara.call("/api/verdict", {"item_id": "x"}, headers={"Origin": "http://evil.example"})
        expect(status == 403, "cross-origin POST refused")

    for name, fn in [("unauthenticated requests", unauthenticated), ("failed sign-in", failed_login),
                     ("rate limit 429", rate_limit),                      ("admin sign-in sets a cookie", admin_login),
                     ("Basic auth is rate-limited and audited", basic_auth_rate_limited),
                     ("admin creates a reviewer (one-time password)", create_reviewer),
                     ("reviewer must choose a password", reviewer_must_change),
                     ("review flow: blind, key, verdict", review_flow), ("admin activity shows it", admin_activity),
                     ("reviewer stats", reviewer_stats),
                     ("activity feed hides answers until a peek", activity_hides_answers_until_peek),
                     ("admin item audit records a peek", admin_peek),
                     ("disabling a reviewer ends their session", disable_kills_session),
                     ("cross-origin POST refused", csrf), ("sign out", logout)]:
        if not run.step(P, name, fn):
            return False
    return True


def find_playwright(arg):
    for cand in [arg, os.environ.get("FARHANG_E2E_PLAYWRIGHT"), DEFAULT_PLAYWRIGHT]:
        if cand and (Path(cand) / "node_modules" / "playwright").is_dir():
            return Path(cand).resolve()
    return None


def browser_phase(run, out, cfg, pw_dir):
    cfg = dict(cfg, playwrightDir=str(pw_dir), outDir=str(out))
    t0 = time.time()
    proc = subprocess.run(["node", str(ROOT / "tests" / "e2e_browser.mjs")], cwd=ROOT,
                          env=dict(os.environ, E2E_CONFIG=json.dumps(cfg)), capture_output=True, text=True, timeout=900)
    (out / "browser.log").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr, encoding="utf-8")
    report = out / "browser-report.json"
    if not report.is_file():
        run.steps.append({"phase": "browser", "name": "browser run", "ok": False, "ms": int(1000 * (time.time() - t0)),
                          "error": f"no browser report (exit {proc.returncode}); see browser.log"})
        print(proc.stdout[-3000:], proc.stderr[-3000:])
        return [], []
    data = json.loads(report.read_text(encoding="utf-8"))
    for s in data["steps"]:
        s["phase"] = "browser"
        run.steps.append(s)
        print(f"  {'ok  ' if s['ok'] else 'FAIL'}  {s['name']}" + ("" if s["ok"] else f": {s.get('error', '')[:300]}"))
    return data.get("screenshots", []), data.get("console", [])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--no-browser", action="store_true", help="HTTP part only")
    ap.add_argument("--playwright", help="directory whose node_modules has playwright (default: $FARHANG_E2E_PLAYWRIGHT)")
    ap.add_argument("--keep", action="store_true", help="keep the temporary server state")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = ROOT / "artifacts" / "e2e-review" / stamp
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="farhang-e2e-"))
    (tmp / "users").write_text(user_line(ADMIN, ADMIN_PW) + "\n", encoding="utf-8")
    run = Run(out)
    started = datetime.now(timezone.utc).isoformat()
    procs = []
    screenshots, console = [], []
    try:
        proc, base = start_server(tmp, free_port())
        procs.append(proc)
        print(f"server at {base} (state in {tmp})\nHTTP:")
        ctx = {}
        http_ok = http_phase(run, base, ctx)
        if args.no_browser:
            pass
        elif not http_ok:
            print("browser: skipped because the HTTP part failed")
        else:
            pw_dir = find_playwright(args.playwright)
            if not pw_dir:
                run.steps.append({"phase": "browser", "name": "find Playwright", "ok": False, "ms": 0,
                                  "error": "Playwright not found: set FARHANG_E2E_PLAYWRIGHT (see this file's docstring)"})
                print("browser: Playwright not found; set FARHANG_E2E_PLAYWRIGHT or pass --no-browser")
            else:
                local_tmp = tmp / "local"
                local_tmp.mkdir()
                lproc, local_base = start_server(local_tmp, free_port(), accounts=False)
                procs.append(lproc)
                print(f"browser (Playwright from {pw_dir}; local-mode server at {local_base}):")
                screenshots, console = browser_phase(run, out, {
                    "base": base, "localBase": local_base, "admin": ADMIN, "adminPassword": ADMIN_PW,
                    "seededReviewer": REVIEWER, "reviewedItem": ctx["ui_audit_item"],
                    "browserItem": ctx["browser_item"], "newUser": "reza", "newUserDisplay": "رضا",
                    "newUserPassword": "reza-chosen-pass-77"}, pw_dir)
    finally:
        for p in procs:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        for log in tmp.rglob("server-*.log"):
            shutil.copy(log, out / log.name)
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    ok = bool(run.steps) and all(s["ok"] for s in run.steps)
    report = {"ok": ok, "started": started, "finished": datetime.now(timezone.utc).isoformat(),
              "passed": sum(s["ok"] for s in run.steps), "failed": sum(not s["ok"] for s in run.steps),
              "steps": run.steps, "screenshots": screenshots, "console": console}
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'PASS' if ok else 'FAIL'}: {report['passed']} passed, {report['failed']} failed; "
          f"{len(screenshots)} screenshots\nartifacts: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
