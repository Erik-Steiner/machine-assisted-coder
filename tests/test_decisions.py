"""Regression tests for coding_store/decisions.py: triage decisions change the
labels the way they say, and undo restores exactly what was there -- including the
original code/rejection row's provenance (created_at/source), which a naive
"re-add a code" undo would overwrite. Covered under CLAUDE.md rule 8's
label-logic exception. Each test uses a throwaway coding.db (rule 11).

Run with: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from coding_store import codes, decisions, schema  # noqa: E402

CTX = {"mode": "top", "engine": "tfidf", "model_version": "tfidf_v1_1", "queue_theme_id": "a"}


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self._orig_db = schema.DB_PATH
        self._tmp = tempfile.TemporaryDirectory()
        schema.set_db_path(Path(self._tmp.name) / "coding.db")

    def tearDown(self):
        schema.DB_PATH = self._orig_db  # restore without init_db() -- never touch the real file
        self._tmp.cleanup()

    def state(self, seg="s1", theme="a"):
        coded = theme in codes.get_codes_for_segment_ids([seg]).get(seg, [])
        rejected = theme in codes.get_negatives_for_segment_ids([seg]).get(seg, [])
        return "coded" if coded else "rejected" if rejected else "none"

    def rows(self, table):
        with schema.get_conn() as conn:
            return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def test_accept_reject_skip(self):
        d = decisions.record_decision("s1", "a", "accept", **CTX)
        self.assertEqual((self.state(), d["prev_state"]), ("coded", "none"))
        self.assertEqual(self.rows("codes")[0]["source"], "review_accept")
        d = decisions.record_decision("s1", "a", "reject", **CTX)
        self.assertEqual((self.state(), d["prev_state"]), ("rejected", "coded"))
        decisions.record_decision("s2", "a", "skip", **CTX)
        self.assertEqual(self.state("s2"), "none")
        self.assertEqual(decisions.round_summary("tfidf_v1_1", "a", "top"),
                         {"accept": 1, "reject": 1, "skip": 1, "total": 3})

    def test_undo_restores_original_code_row(self):
        codes.add_code("s1", "a", source="manual", note="first pass")
        original = self.rows("codes")[0]
        d = decisions.record_decision("s1", "a", "reject", **CTX)  # likely-mislabel flip
        decisions.undo(d["decision_id"])
        self.assertEqual(self.state(), "coded")
        restored = [r for r in self.rows("codes") if r["deleted_at"] is None]
        self.assertEqual(len(restored), 1)
        self.assertEqual((restored[0]["code_id"], restored[0]["created_at"], restored[0]["source"]),
                         (original["code_id"], original["created_at"], "manual"))
        self.assertIsNotNone(self.rows("negatives")[0]["deleted_at"])  # the rejection is gone again

    def test_undo_restores_original_rejection(self):
        codes.add_negative("s1", "a", source="manual")
        d = decisions.record_decision("s1", "a", "accept", **CTX)
        decisions.undo(d["decision_id"])
        self.assertEqual(self.state(), "rejected")
        self.assertEqual([r["source"] for r in self.rows("negatives") if r["deleted_at"] is None], ["manual"])

    def test_undo_of_new_label_leaves_nothing(self):
        d = decisions.record_decision("s1", "a", "accept", **CTX)
        decisions.undo(d["decision_id"])
        self.assertEqual(self.state(), "none")
        self.assertEqual(decisions.round_summary("tfidf_v1_1", "a", "top")["total"], 0)
        self.assertIsNotNone(self.rows("review_decisions")[0]["undone_at"])  # kept, marked undone

    def test_only_latest_decision_can_be_undone(self):
        first = decisions.record_decision("s1", "a", "accept", **CTX)
        decisions.record_decision("s1", "a", "reject", **CTX)
        with self.assertRaises(ValueError):
            decisions.undo(first["decision_id"])
        with self.assertRaises(ValueError):
            decisions.record_decision("s1", "a", "maybe", **CTX)

    def test_overlap_decision_targets_other_theme(self):
        codes.add_code("s1", "a")
        d = decisions.record_decision("s1", "b", "reject", **{**CTX, "mode": "confusion"})
        self.assertEqual((self.state(theme="a"), self.state(theme="b")), ("coded", "rejected"))
        self.assertEqual((d["theme_id"], d["queue_theme_id"], d["round_id"]), ("b", "a", "tfidf_v1_1:a:confusion"))


if __name__ == "__main__":
    unittest.main()
