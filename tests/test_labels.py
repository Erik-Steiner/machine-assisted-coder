"""Regression tests for label bookkeeping: codes vs. explicit rejections
(coding_store/codes.py's add_code/add_negative) and how theme merges carry both
(coding_store/themes.py's merge_themes). Mistakes here don't raise errors; they
quietly leave a segment both coded and rejected, or lose a rejection, and later
train or queue the wrong thing -- see CLAUDE.md rule 8's named exceptions.
Also the hierarchy rules (roadmap A2, coding_store/labels.py); A6 (negative modes) will add its tests here.

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

import classifier  # noqa: E402
from coding_store import codes, labels, review, schema, schemes, segments, themes  # noqa: E402


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


class HierarchyRollUpTests(TempDbTestCase):
    """coding_store/labels.py: Market > Pricing > Discounts, plus a sibling Competition."""

    def setUp(self):
        super().setUp()
        schemes.create_scheme("S")
        self.market = themes.create_theme("Market", scheme_id="s")["theme_id"]
        self.pricing = themes.create_theme("Pricing", scheme_id="s", parent_theme_id=self.market)["theme_id"]
        self.discounts = themes.create_theme("Discounts", scheme_id="s", parent_theme_id=self.pricing)["theme_id"]
        self.competition = themes.create_theme("Competition", scheme_id="s", parent_theme_id=self.market)["theme_id"]

    def test_codes_roll_up_through_every_ancestor(self):
        codes.add_code("s1", self.discounts)
        codes.add_code("s2", self.competition)
        codes.add_code("s3", self.market)
        self.assertEqual(labels.effective_positive_segment_ids(self.market), {"s1", "s2", "s3"})
        self.assertEqual(labels.effective_positive_segment_ids(self.pricing), {"s1"})
        self.assertEqual(labels.effective_positive_segment_ids(self.competition), {"s2"})

    def test_codes_never_roll_down_or_sideways(self):
        codes.add_code("s1", self.market)
        self.assertEqual(labels.effective_positive_segment_ids(self.pricing), set())
        self.assertEqual(labels.effective_positive_segment_ids(self.competition), set())

    def test_archived_subcode_still_counts(self):
        codes.add_code("s1", self.pricing)
        themes.set_theme_status(self.pricing, "archived")
        self.assertEqual(labels.effective_positive_segment_ids(self.market), {"s1"})

    def test_rejection_flows_down_not_up(self):
        codes.add_negative("s1", self.market)
        codes.add_negative("s2", self.pricing)
        self.assertEqual(labels.effective_rejected_segment_ids(self.discounts), {"s1", "s2"})
        self.assertEqual(labels.effective_rejected_segment_ids(self.market), {"s1"})
        self.assertEqual(labels.effective_rejected_segment_ids(self.competition), {"s1"})

    def test_positive_wins_over_inherited_rejection(self):
        codes.add_negative("s1", self.market)  # "not Market"...
        codes.add_code("s1", self.pricing)     # ...but coded Pricing, which implies Market
        self.assertEqual(labels.effective_positive_segment_ids(self.market), {"s1"})
        self.assertEqual(labels.effective_rejected_segment_ids(self.market), set())
        self.assertEqual(labels.effective_rejected_segment_ids(self.pricing), set())
        self.assertEqual(labels.effective_rejected_segment_ids(self.discounts), {"s1"})

    def test_build_labels_uses_roll_up(self):
        codes.add_code("s1", self.discounts)
        codes.add_code("s2", self.market)
        y, n_pos = classifier.build_labels(self.market, ["s1", "s2", "s3"])
        self.assertEqual(list(y), [1, 1, 0])
        self.assertEqual(n_pos, 2)

    def test_progress_counts_match_training(self):
        segments.upsert_segments([  # get_progress() counts codes on segments that exist
            {"segment_id": sid, "item_id": "i1", "dataset_id": "d1", "seg_index": i, "text": "x"}
            for i, sid in enumerate(["s1", "s2"])
        ])
        codes.add_code("s1", self.discounts)
        codes.add_code("s1", self.pricing)  # same segment twice under one subtree: counted once
        codes.add_code("s2", self.competition)
        codes.add_code("s2", self.competition, coder="other")  # two coders: still one segment
        by_theme = review.get_progress(scheme_id="s")["by_theme"]
        self.assertEqual(by_theme[self.market]["count"], 2)
        self.assertEqual(by_theme[self.pricing]["count"], 1)
        self.assertEqual(by_theme[self.competition]["count"], 1)

    def test_roll_up_counts_is_cycle_safe(self):
        counts = labels.roll_up_counts([("s1", "a")], {"a": "b", "b": "a"})
        self.assertEqual(counts, {"a": 1, "b": 1})


if __name__ == "__main__":
    unittest.main()
