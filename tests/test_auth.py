import base64
import io
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

from farhang.auth import Users, hash_password, user_line, verify_password
from farhang.data import ROOT, load_items
from farhang.review import ReviewStore, make_handler


def basic(name, password):
    return "Basic " + base64.b64encode(f"{name}:{password}".encode()).decode()


class PasswordTest(unittest.TestCase):
    def test_hash_and_verify(self):
        encoded = hash_password("correct horse", iterations=1000)
        self.assertTrue(verify_password("correct horse", encoded))
        self.assertFalse(verify_password("wrong horse", encoded))
        self.assertFalse(verify_password("x", "garbage"))

    def test_users_file_reloads(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "users"
            path.write_text("# reviewers\n" + user_line("Maryam", "pass-maryam-1") + "\n", encoding="utf-8")
            users = Users(path)
            self.assertEqual(users.check(basic("Maryam", "pass-maryam-1")), "maryam")
            self.assertEqual(users.check(basic("maryam", "pass-maryam-1")), "maryam")  # cached path
            self.assertIsNone(users.check(basic("maryam", "nope")))
            self.assertIsNone(users.check(basic("ali", "pass-maryam-1")))
            self.assertIsNone(users.check("Bearer x"))
            path.write_text(user_line("Ali", "pass-ali-123") + "\n", encoding="utf-8")
            import os
            os.utime(path, ns=(1, 1))  # force a different mtime on fast filesystems
            self.assertIsNone(users.check(basic("maryam", "pass-maryam-1")))  # removed accounts stop working
            self.assertEqual(users.check(basic("ali", "pass-ali-123")), "ali")


class SignedInServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "data" / "seed", self.tmp / "data" / "seed")
        (self.tmp / "users").write_text(user_line("maryam", "pass-maryam-1") + "\n", encoding="utf-8")
        self.store = ReviewStore(self.tmp / "data", self.tmp / "validation")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store, Users(self.tmp / "users")))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        self.item = next(i for i in load_items(ROOT / "data" / "seed") if i["format"] == "mcq")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp)

    def call(self, path, body=None, auth=None, origin=None):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = auth
        if origin:
            headers["Origin"] = origin
        req = urllib.request.Request(f"http://{self.host}{path}", data=json.dumps(body).encode() if body else None,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def test_login_required_except_health(self):
        self.assertEqual(self.call("/healthz")[0], 200)
        code, _, headers = self.call("/")
        self.assertEqual(code, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])
        self.assertEqual(self.call("/api/items?reviewer=maryam", auth=basic("maryam", "bad"))[0], 401)
        self.assertEqual(self.call("/api/blind", {"reviewer": "x", "item_id": self.item["id"], "answer": 0})[0], 401)

    def test_signed_in_name_is_the_reviewer(self):
        auth = basic("maryam", "pass-maryam-1")
        self.assertEqual(json.loads(self.call("/api/whoami", auth=auth)[1]), {"reviewer": "maryam", "signed_in": True})
        # The body claims to be someone else; the review is filed under the signed-in account.
        code, _, _ = self.call("/api/blind", {"reviewer": "ali", "item_id": self.item["id"], "answer": 0}, auth=auth,
                               origin=f"https://{self.host}")  # behind a TLS ingress the origin is https
        self.assertEqual(code, 200)
        self.assertEqual([p.name for p in (self.tmp / "validation").iterdir()], ["maryam.jsonl"])
        self.assertEqual(self.call("/api/blind", {"item_id": self.item["id"]}, auth=auth, origin="https://evil.example")[0], 403)
        code, data, headers = self.call("/api/export", auth=auth)
        self.assertEqual((code, headers["Content-Type"]), (200, "application/zip"))
        self.assertEqual(zipfile.ZipFile(io.BytesIO(data)).namelist(), ["validation/maryam.jsonl"])


if __name__ == "__main__":
    unittest.main()
