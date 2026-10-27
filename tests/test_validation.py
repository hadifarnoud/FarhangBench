import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from farhang.data import ROOT, load_items
from farhang.review import ReviewStore, make_handler
from farhang.validation import (blind_correct, content_hash, item_status, load_records, merge_reviews, reviewer_slug,
                                statuses)

SEED = load_items(ROOT / "data" / "seed")


def first(fmt):
    return next(i for i in SEED if i["format"] == fmt)


def review(verdict, blind=True):
    return {"blind_answer": "x", "blind_correct": blind, "verdict": verdict}


class StatusRulesTest(unittest.TestCase):
    item = {"id": "L1-0001", "metadata": {"status": "draft"}}

    def status(self, *verdicts):
        return item_status(self.item, {f"r{i}": review(v) for i, v in enumerate(verdicts)})["status"]

    def test_rules(self):
        self.assertEqual(self.status(), "unreviewed")
        self.assertEqual(self.status("correct"), "in_review")
        self.assertEqual(self.status("wrong"), "disputed")
        self.assertEqual(self.status("correct", "correct"), "validated")
        self.assertEqual(self.status("correct", "unsure"), "disputed")         # 1 of 2 is below two thirds
        self.assertEqual(self.status("correct", "correct", "wrong"), "validated")  # 2 of 3
        self.assertEqual(self.status("correct", "wrong", "unsure"), "disputed")

    def test_outdated(self):
        def rv(verdict, outdated):
            return dict(review(verdict), flags=["outdated"] if outdated else [])
        status = lambda *rs: item_status(self.item, {f"r{i}": r for i, r in enumerate(rs)})["status"]
        self.assertEqual(status(rv("wrong", True), rv("wrong", True)), "outdated")
        self.assertEqual(status(rv("wrong", True), rv("correct", False)), "outdated")  # half is enough
        self.assertEqual(status(rv("correct", False), rv("correct", False), rv("wrong", True)), "validated")
        self.assertEqual(status(rv("wrong", True)), "disputed")  # one review isn't enough to drop it

    def test_author_reviews_do_not_count(self):
        item = {"id": "L1-0001", "metadata": {"status": "draft", "author": "Maryam"}}
        st = item_status(item, {"maryam": review("correct"), "ali": review("correct")})
        self.assertEqual((st["status"], st["verdicts"]), ("in_review", 1))

    def test_retired(self):
        item = {"id": "L1-0001", "metadata": {"status": "retired"}}
        self.assertEqual(item_status(item, {"a": review("correct"), "b": review("correct")})["status"], "retired")

    def test_blind_correct(self):
        mcq = first("mcq")
        self.assertTrue(blind_correct(mcq, mcq["answer"]["index"]))
        self.assertFalse(blind_correct(mcq, None))
        judgment = first("judgment")
        self.assertTrue(blind_correct(judgment, judgment["answer"]["label"]))
        short = first("short_answer")
        self.assertTrue(blind_correct(short, short["answer"]["accept"][0]))
        self.assertIsNone(blind_correct(first("generation"), ""))

    def test_hash_ignores_status_but_not_content(self):
        item = first("mcq")
        h = content_hash(item)
        self.assertEqual(h, content_hash(dict(item, metadata=dict(item["metadata"], status="validated"), _line=3)))
        self.assertNotEqual(h, content_hash(dict(item, prompt=item["prompt"] + "؟")))

    def test_first_blind_answer_stands_and_latest_verdict_wins(self):
        recs = [{"type": "blind", "reviewer": "a", "item_id": "x", "item_hash": "h", "answer": 1, "correct": False, "time": "1"},
                {"type": "blind", "reviewer": "a", "item_id": "x", "item_hash": "h", "answer": 2, "correct": True, "time": "2"},
                {"type": "verdict", "reviewer": "a", "item_id": "x", "item_hash": "h", "verdict": "wrong", "time": "3"},
                {"type": "verdict", "reviewer": "a", "item_id": "x", "item_hash": "h", "verdict": "correct", "time": "4"}]
        r = merge_reviews(recs)[("x", "h")]["a"]
        self.assertEqual((r["blind_answer"], r["blind_correct"], r["verdict"]), (1, False, "correct"))

    def test_reviewer_slug(self):
        self.assertEqual(reviewer_slug("  Maryam R. "), "maryam-r")
        self.assertEqual(reviewer_slug("مریم"), "مریم")
        self.assertEqual(reviewer_slug("../../etc"), "etc")


class ReviewServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "data" / "seed", self.tmp / "data" / "seed")
        self.reviews = self.tmp / "validation"
        self.store = ReviewStore(self.tmp / "data", self.reviews)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.item = first("mcq")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp)

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_blind_then_verdict(self):
        code, items = self.call("/api/items?reviewer=Ali")
        self.assertEqual(code, 200)
        listed = next(i for i in items if i["id"] == self.item["id"])
        self.assertNotIn("answer", listed)
        self.assertEqual(listed["status"], "unreviewed")
        # The key is withheld, and a verdict refused, until a blind answer is recorded.
        self.assertEqual(self.call(f"/api/key?reviewer=ali&id={self.item['id']}")[0], 403)
        self.assertEqual(self.call("/api/verdict", {"reviewer": "ali", "item_id": self.item["id"], "verdict": "correct"})[0], 403)

        gold = self.item["answer"]["index"]
        code, key = self.call("/api/blind", {"reviewer": "Ali", "item_id": self.item["id"], "answer": (gold + 1) % 4})
        self.assertEqual((code, key["answer"]["index"], key["review"]["blind_correct"]), (200, gold, False))
        # A second blind answer after seeing the key does not replace the first.
        self.call("/api/blind", {"reviewer": "ali", "item_id": self.item["id"], "answer": gold})
        self.assertFalse(self.call(f"/api/key?reviewer=ali&id={self.item['id']}")[1]["review"]["blind_correct"])

        code, _ = self.call("/api/verdict", {"reviewer": "ali", "item_id": self.item["id"], "verdict": "correct",
                                             "flags": ["ambiguous"], "comment": " see Dehkhoda "})
        self.assertEqual(code, 200)
        self.assertEqual(self.call("/api/verdict", {"reviewer": "ali", "item_id": self.item["id"], "verdict": "maybe"})[0], 400)
        self.call("/api/blind", {"reviewer": "sara", "item_id": self.item["id"], "answer": gold})
        self.call("/api/verdict", {"reviewer": "sara", "item_id": self.item["id"], "verdict": "correct"})

        st = statuses([self.item], self.reviews)[self.item["id"]]
        self.assertEqual((st["status"], st["human_accuracy"], st["flags"]), ("validated", 0.5, ["ambiguous"]))
        self.assertEqual({p.name for p in self.reviews.iterdir()}, {"ali.jsonl", "sara.jsonl"})
        self.assertEqual(len(load_records(self.reviews)), 4)  # the repeated blind answer is not written
        progress = self.call("/api/progress")[1]
        row = next(r for r in progress["rows"] if r["group"] == self.item["task"])
        self.assertEqual(row["validated"], 1)
        self.assertEqual([p["id"] for p in progress["problems"]], [self.item["id"]])  # flagged

    def test_editing_an_item_resets_its_reviews(self):
        for who in ("a", "b"):
            self.call("/api/blind", {"reviewer": who, "item_id": self.item["id"], "answer": 0})
            self.call("/api/verdict", {"reviewer": who, "item_id": self.item["id"], "verdict": "correct"})
        self.assertEqual(statuses([self.item], self.reviews)[self.item["id"]]["status"], "validated")
        edited = dict(self.item, prompt=self.item["prompt"] + " ")
        self.assertEqual(statuses([edited], self.reviews)[self.item["id"]]["status"], "unreviewed")

    def test_page_and_cross_origin(self):
        with urllib.request.urlopen(self.base + "/") as resp:
            self.assertIn("FarhangBench review", resp.read().decode())
        req = urllib.request.Request(self.base + "/api/blind", data=b"{}", headers={"Origin": "http://evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
