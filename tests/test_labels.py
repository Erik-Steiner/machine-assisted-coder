"""Regression tests for label bookkeeping: codes vs. explicit rejections
(coding_store/codes.py's add_code/add_negative) and how theme merges carry both
(coding_store/themes.py's merge_themes). Mistakes here don't raise errors; they
quietly leave a segment both coded and rejected, or lose a rejection, and later
train or queue the wrong thing -- see CLAUDE.md rule 8's named exceptions.
Roadmap A2/A6 (hierarchy roll-up, negative modes) will add their tests here.

Every test uses a throwaway coding.db in a temp directory, never a real project
(CLAUDE.md rule 11), and restores schema.DB_PATH afterwards.

Run with: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from coding_store import codes, schema, schemes, themes  # noqa: E402


class TempDbTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_db = schema.DB_PATH
        self._tmp = tempfile.TemporaryDirectory()
        schema.set_db_path(Path(self._tmp.name) / "coding.db")

    def tearDown(self):
        schema.DB_PATH = self._orig_db  # restore without init_db() -- never touch the real file
        self._tmp.cleanup()

    def active_codes(self, seg):
        return sorted(codes.get_codes_for_segment_ids([seg]).get(seg, []))

    def active_rejections(self, seg):
        return sorted(codes.get_negatives_for_segment_ids([seg]).get(seg, []))

    def all_rows(self, table):
        with schema.get_conn() as conn:
            return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]


class CodeRejectionExclusivityTests(TempDbTestCase):
    def test_rejecting_cancels_code(self):
        codes.add_code("s1", "a")
        codes.add_negative("s1", "a", source="review_reject")
        self.assertEqual(self.active_codes("s1"), [])
        self.assertEqual(self.active_rejections("s1"), ["a"])

    def test_coding_cancels_rejection(self):
        codes.add_negative("s1", "a")
        codes.add_code("s1", "a")
        self.assertEqual(self.active_codes("s1"), ["a"])
        self.assertEqual(self.active_rejections("s1"), [])

    def test_other_themes_and_coders_untouched(self):
        codes.add_code("s1", "b")
        codes.add_code("s1", "a", coder="other")
        codes.add_negative("s1", "a")
        self.assertEqual(self.active_codes("s1"), ["a", "b"])  # other coder's "a" and own "b" survive
        self.assertEqual(self.active_rejections("s1"), ["a"])

    def test_nothing_is_hard_deleted(self):
        codes.add_code("s1", "a")
        codes.add_negative("s1", "a")
        codes.remove_negative("s1", "a")
        self.assertEqual(len(self.all_rows("codes")), 1)
        self.assertEqual(len(self.all_rows("negatives")), 1)
        self.assertIsNotNone(self.all_rows("negatives")[0]["deleted_at"])

    def test_re_rejecting_reactivates_same_row(self):
        codes.add_negative("s1", "a", source="manual")
        codes.remove_negative("s1", "a")
        codes.add_negative("s1", "a", source="review_reject")
        rows = self.all_rows("negatives")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["deleted_at"])
        self.assertEqual(rows[0]["source"], "review_reject")

    def test_removing_rejection_does_not_restore_code(self):
        codes.add_code("s1", "a")
        codes.add_negative("s1", "a")
        codes.remove_negative("s1", "a")
        self.assertEqual(self.active_codes("s1"), [])
        self.assertEqual(self.active_rejections("s1"), [])


class MergeTests(TempDbTestCase):
    def setUp(self):
        super().setUp()
        schemes.create_scheme("S")
        self.src = themes.create_theme("Source", scheme_id="s")["theme_id"]
        self.tgt = themes.create_theme("Target", scheme_id="s")["theme_id"]

    def test_rejection_moves_with_provenance(self):
        codes.add_negative("s1", self.src, source="review_reject", note="n")
        themes.merge_themes(self.src, self.tgt)
        self.assertEqual(self.active_rejections("s1"), [self.tgt])
        row = [r for r in self.all_rows("negatives") if r["deleted_at"] is None][0]
        self.assertEqual((row["source"], row["note"]), ("review_reject", "n"))

    def test_code_on_target_wins_over_moved_rejection(self):
        codes.add_code("s1", self.tgt)
        codes.add_negative("s1", self.src)
        themes.merge_themes(self.src, self.tgt)
        self.assertEqual(self.active_codes("s1"), [self.tgt])
        self.assertEqual(self.active_rejections("s1"), [])

    def test_moved_code_wins_over_target_rejection(self):
        codes.add_code("s1", self.src)
        codes.add_negative("s1", self.tgt)
        themes.merge_themes(self.src, self.tgt)
        self.assertEqual(self.active_codes("s1"), [self.tgt])
        self.assertEqual(self.active_rejections("s1"), [])

    def test_soft_deleted_target_rejection_is_reactivated(self):
        # negatives' UNIQUE covers soft-deleted rows, so re-pointing would collide.
        codes.add_negative("s1", self.tgt)
        codes.remove_negative("s1", self.tgt)
        codes.add_negative("s1", self.src, source="review_reject")
        themes.merge_themes(self.src, self.tgt)
        self.assertEqual(self.active_rejections("s1"), [self.tgt])
        self.assertEqual(len(self.all_rows("negatives")), 2)  # nothing hard-deleted

    def test_duplicate_rejection_is_soft_deleted(self):
        codes.add_negative("s1", self.src)
        codes.add_negative("s1", self.tgt)
        themes.merge_themes(self.src, self.tgt)
        self.assertEqual(self.active_rejections("s1"), [self.tgt])
        self.assertEqual(len(self.all_rows("negatives")), 2)


if __name__ == "__main__":
    unittest.main()
