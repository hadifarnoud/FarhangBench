import json
import tempfile
import unittest
from pathlib import Path

from farhang.report import effective_source_weights, headline
from farhang.weights import DEFAULT_PATH, Weights


def rec(task, source, score):
    md = {} if source == "farhangbench" else {"source": source}
    return {"task": task, "track": task[0], "score": score, "metadata": md}


class WeightsTest(unittest.TestCase):
    def test_equal_weights_macro_average_sources(self):
        records = [rec("L3_idioms_proverbs", "farhangbench", 1.0)] + \
                  [rec("L3_idioms_proverbs", "masalbench_ctx", 0.0)] * 100
        _, _, tasks = headline(records, Weights.equal())
        self.assertAlmostEqual(tasks["L3_idioms_proverbs"], 0.5)

    def test_source_weights_and_shrinkage(self):
        w = Weights(sources={"a": 2.0, "b": 1.0}, shrinkage_items=10)
        self.assertAlmostEqual(w.source("a", 10), 1.0)   # 2.0 × 10 / 20
        self.assertAlmostEqual(w.source("b", 90), 0.9)   # 1.0 × 90 / 100
        records = [rec("P1_verse_recall", "a", 1.0)] * 10 + [rec("P1_verse_recall", "b", 0.0)] * 90
        _, _, tasks = headline(records, w)
        self.assertAlmostEqual(tasks["P1_verse_recall"], 1.0 / 1.9)
        shares = effective_source_weights(records, w)["P1_verse_recall"]
        self.assertAlmostEqual(sum(shares.values()), 1.0)

    def test_track_weights(self):
        records = [rec("L1_grammar_usage", "farhangbench", 1.0), rec("W1_orthography_punctuation", "farhangbench", 0.0)]
        overall, _, _ = headline(records, Weights(tracks={"L": 3.0}))
        self.assertAlmostEqual(overall, 75.0)

    def test_shipped_config_loads_and_names_known_sources(self):
        w = Weights.load(DEFAULT_PATH)
        self.assertGreater(w.sources["farhangbench"], w.sources["blend"])
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "w.json"
            p.write_text(json.dumps({"sources": {"x": 0.5}}), encoding="utf-8")
            self.assertEqual(Weights.load(p).source("x", 5), 0.5)


if __name__ == "__main__":
    unittest.main()
