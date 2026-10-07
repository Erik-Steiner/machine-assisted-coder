"""Regression tests for coding_store/queues.py: which passages each Review queue
mode shows, in what order. A mistake here doesn't error; it quietly shows the
researcher the wrong passages (already-decided ones, or a biased "random" audit),
which skews both the coding and the precision estimates built on it. Covered under
CLAUDE.md rule 8's label-logic exception.

order_queue() is pure; QueueEndToEndTests use a throwaway coding.db (rule 11).

Run with: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from coding_store import codes, model_runs, queues, reviewed, schema, schemes, segments, themes  # noqa: E402


def rows(*triples):
    return [{"segment_id": s, "score": sc, "oof_score": oof} for s, sc, oof in triples]


class OrderQueueTests(unittest.TestCase):
    ROWS = rows(("a", 0.9, 0.8), ("b", 0.7, 0.1), ("c", 0.56, 0.6), ("d", 0.47, 0.7), ("e", 0.1, None))

    def ids(self, out):
        return [r["segment_id"] for r in out]

    def test_top_excludes_decided(self):
        out = queues.order_queue("top", self.ROWS, 0.5, positives={"a"}, negatives={"d"})
        self.assertEqual(self.ids(out), ["b", "c", "e"])

    def test_uncertain_orders_by_distance_to_threshold(self):
        out = queues.order_queue("uncertain", self.ROWS, 0.5, positives=set(), negatives=set())
        self.assertEqual(self.ids(out)[:3], ["d", "c", "b"])  # 0.03, 0.06, 0.20 from the threshold

    def test_audit_is_above_threshold_undecided_and_stable(self):
        a = queues.order_queue("audit", self.ROWS, 0.5, positives={"a"}, negatives=set(), seed="run1")
        b = queues.order_queue("audit", list(reversed(self.ROWS)), 0.5, positives={"a"}, negatives=set(), seed="run1")
        self.assertEqual(sorted(self.ids(a)), ["b", "c"])
        self.assertEqual(self.ids(a), self.ids(b))  # same seed -> same order, whatever the input order

    def test_suspect_ranks_disagreement_and_needs_oof(self):
        out = queues.order_queue("suspect", self.ROWS, 0.5, positives={"b", "e"}, negatives={"d"})
        self.assertEqual(self.ids(out), ["b", "d"])  # coded b at 0.1 (0.9 off), judged-negative d at 0.7
        self.assertEqual([r["label"] for r in out], ["positive", "negative"])  # e has no oof -> left out

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            queues.order_queue("bogus", self.ROWS, 0.5, set(), set())


class QueueEndToEndTests(unittest.TestCase):
    def setUp(self):
        self._orig_db = schema.DB_PATH
        self._tmp = tempfile.TemporaryDirectory()
        schema.set_db_path(Path(self._tmp.name) / "coding.db")
        schemes.create_scheme("S")
        self.a = themes.create_theme("A", scheme_id="s")["theme_id"]
        self.b = themes.create_theme("B", scheme_id="s")["theme_id"]
        self.child = themes.create_theme("A child", scheme_id="s", parent_theme_id=self.a)["theme_id"]
        ids = [f"s{i}" for i in range(6)]
        segments.upsert_segments([{"segment_id": s, "item_id": "i1", "dataset_id": "d1", "seg_index": i,
                                   "text": f"text {s}", "word_count": 5} for i, s in enumerate(ids)])
        metrics = {"n_pos": 1, "n_neg": 1, "precision": 1, "recall": 1, "f1": 1, "pr_auc": 1, "threshold": 0.5}
        for theme, scores in ((self.a, [0.9, 0.8, 0.6, 0.4, 0.2, 0.1]), (self.b, [0.95, 0.2, 0.7, 0.1, 0.9, 0.3]),
                              (self.child, [0.9] * 6)):
            model_runs.save_model_run("tfidf_v1_1", theme, "2026-01-01", metrics)
            model_runs.save_predictions("tfidf_v1_1", theme, ids, scores, [0.5] * 6)
        codes.add_code("s0", self.a)
        codes.add_code("s2", self.a)
        codes.add_negative("s1", self.a)

    def tearDown(self):
        schema.DB_PATH = self._orig_db  # restore without init_db() -- never touch the real file
        self._tmp.cleanup()

    def test_top_page_and_total(self):
        q = queues.get_queue(self.a, "top", limit=2)
        self.assertEqual((q["total"], [i["segment_id"] for i in q["items"]]), (3, ["s3", "s4"]))
        self.assertEqual(q["model_version"], "tfidf_v1_1")
        q2 = queues.get_queue(self.a, "top", offset=2, limit=2)
        self.assertEqual([i["segment_id"] for i in q2["items"]], ["s5"])

    def test_reviewed_mark_counts_as_decided(self):
        reviewed.set_item_review("d1", "i1", "s", True)
        self.assertEqual(queues.get_queue(self.a, "top")["total"], 0)

    def test_confusion_skips_own_subcodes_and_decided(self):
        codes.add_negative("s2", self.b)  # s2 high for B but already rejected for B
        q = queues.get_queue(self.a, "confusion")
        self.assertEqual([(i["segment_id"], i["other_theme_id"]) for i in q["items"]], [("s0", self.b)])

    def test_confusion_ignores_implicit_marks(self):
        reviewed.set_item_review("d1", "i1", "s", True)  # s0 now implicitly "not B"
        q = queues.get_queue(self.a, "confusion")
        self.assertEqual([i["segment_id"] for i in q["items"]], ["s0", "s2"])

    def test_segment_scores_and_top_terms(self):
        scores = model_runs.get_scores_for_segments(["s0", "s3"], [self.a, self.b, "unknown"])
        self.assertEqual(scores["s0"][self.a]["score"], 0.9)
        self.assertEqual(scores["s3"][self.b]["score"], 0.1)
        self.assertEqual(set(scores["s0"]), {self.a, self.b})  # no run for "unknown" -> absent
        self.assertEqual(model_runs.get_scores_for_segments(["s0"], [self.a], engine="embed"), {})
        model_runs.save_top_terms("tfidf_v1_1", self.a, [("growth", 1.2), ("revenue", 0.9)], [("weather", -1.0)])
        self.assertEqual(queues.get_queue(self.a, "top")["top_terms"], ["growth", "revenue"])

    def test_no_run_for_engine(self):
        q = queues.get_queue(self.a, "top", engine="embed")
        self.assertEqual((q["model_version"], q["items"]), (None, []))


if __name__ == "__main__":
    unittest.main()
