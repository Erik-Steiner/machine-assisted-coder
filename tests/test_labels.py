"""Regression tests for label bookkeeping: codes vs. explicit rejections
(coding_store/codes.py's add_code/add_negative) and how theme merges carry both
(coding_store/themes.py's merge_themes). Mistakes here don't raise errors; they
quietly leave a segment both coded and rejected, or lose a rejection, and later
train or queue the wrong thing -- see CLAUDE.md rule 8's named exceptions.
Also the hierarchy rules (roadmap A2, coding_store/labels.py) and the `reviewed` negative mode (A6).

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
from coding_store import codes, labels, model_runs, review, reviewed, schema, schemes, segments, themes  # noqa: E402


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


def add_segments(item_id, segment_ids, dataset_id="d1", text="some words about the topic here"):
    segments.upsert_segments([
        {"segment_id": sid, "item_id": item_id, "dataset_id": dataset_id, "seg_index": i,
         "text": text, "word_count": len(text.split())}
        for i, sid in enumerate(segment_ids)
    ])


class ReviewedNegativeTests(TempDbTestCase):
    """labels.reviewed_negative_segment_ids() -- the `reviewed` negative mode (roadmap A6)."""

    def setUp(self):
        super().setUp()
        schemes.create_scheme("S")
        schemes.create_scheme("Other")
        add_segments("i1", ["a1", "a2", "a3"])
        add_segments("i2", ["b1", "b2"])
        self.early = themes.create_theme("Early", scheme_id="s")["theme_id"]

    def test_marked_item_uncoded_segments_are_negatives(self):
        codes.add_code("a1", self.early)
        reviewed.set_item_review("d1", "i1", "s", True)
        self.assertEqual(labels.reviewed_negative_segment_ids(self.early), {"a2", "a3"})

    def test_unread_items_are_not_negatives(self):
        reviewed.set_item_review("d1", "i1", "s", True)
        self.assertFalse({"b1", "b2"} & labels.reviewed_negative_segment_ids(self.early))

    def test_mark_only_counts_for_themes_that_already_existed(self):
        reviewed.set_item_review("d1", "i1", "s", True)
        late = themes.create_theme("Late", scheme_id="s")["theme_id"]
        self.assertEqual(labels.reviewed_negative_segment_ids(late), set())
        # Re-marking keeps the original time; unmark + mark again covers the new theme.
        reviewed.set_item_review("d1", "i1", "s", True)
        self.assertEqual(labels.reviewed_negative_segment_ids(late), set())
        reviewed.set_item_review("d1", "i1", "s", False)
        reviewed.set_item_review("d1", "i1", "s", True)
        self.assertEqual(labels.reviewed_negative_segment_ids(late), {"a1", "a2", "a3"})

    def test_unmarking_withdraws(self):
        reviewed.set_item_review("d1", "i1", "s", True)
        reviewed.set_item_review("d1", "i1", "s", False)
        self.assertEqual(labels.reviewed_negative_segment_ids(self.early), set())
        self.assertEqual(len(self.all_rows("item_reviews")), 1)  # history kept, not deleted

    def test_mark_in_another_scheme_does_not_count(self):
        reviewed.set_item_review("d1", "i1", "other", True)
        self.assertEqual(labels.reviewed_negative_segment_ids(self.early), set())

    def test_none_confirmation_and_rejection_count(self):
        reviewed.set_segment_none("b1", "s", True)
        codes.add_negative("b2", self.early)
        self.assertEqual(labels.reviewed_negative_segment_ids(self.early), {"b1", "b2"})
        reviewed.set_segment_none("b1", "s", False)
        self.assertEqual(labels.reviewed_negative_segment_ids(self.early), {"b2"})

    def test_positive_wins_over_reviewed_negative(self):
        reviewed.set_item_review("d1", "i1", "s", True)
        child = themes.create_theme("Child", scheme_id="s", parent_theme_id=self.early)["theme_id"]
        codes.add_code("a2", child)  # coded with a sub-code after the mark: positive for Early
        self.assertEqual(labels.reviewed_negative_segment_ids(self.early), {"a1", "a3"})

    def test_training_labels_by_mode(self):
        codes.add_code("a1", self.early)
        reviewed.set_item_review("d1", "i1", "s", True)
        ids = ["a1", "a2", "a3", "b1", "b2"]
        idx, y, n_pos, n_neg = classifier.training_labels(self.early, ids, "reviewed")
        self.assertEqual([ids[i] for i in idx], ["a1", "a2", "a3"])  # unread b1/b2 left out
        self.assertEqual((list(y), n_pos, n_neg), ([1, 0, 0], 1, 2))
        idx, y, n_pos, n_neg = classifier.training_labels(self.early, ids, "all_uncoded")
        self.assertEqual((len(idx), n_pos, n_neg), (5, 1, 4))
        with self.assertRaises(ValueError):
            classifier.training_labels(self.early, ids, "bogus")


class ReviewedTrainingPassTests(TempDbTestCase):
    """A small end-to-end training pass in each negative mode."""

    def setUp(self):
        super().setUp()
        schemes.create_scheme("S")
        self.theme = themes.create_theme("Growth", scheme_id="s")["theme_id"]
        pos_text = "revenue growth expanded quickly across new markets this year strongly"
        neg_text = "the weather and the office kitchen were discussed at length today calmly"
        unread_text = "an unrelated passage nobody has read about trains and maps and rivers"
        add_segments("pos", [f"p{i:02d}" for i in range(30)], text=pos_text)
        add_segments("neg", [f"n{i:02d}" for i in range(60)], text=neg_text)
        add_segments("unread", [f"u{i:02d}" for i in range(40)], text=unread_text)
        for i in range(30):
            codes.add_code(f"p{i:02d}", self.theme)
        reviewed.set_item_review("d1", "neg", "s", True)

    def test_reviewed_mode_fits_on_judged_rows_and_scores_everything(self):
        result = classifier.run_training_pass(negative_mode="reviewed")
        self.assertEqual(result["results"][0]["status"], "trained")
        self.assertEqual((result["results"][0]["n_pos"], result["results"][0]["n_neg"]), (30, 60))
        with schema.get_conn() as conn:
            rows = conn.execute(
                "SELECT segment_id, oof_score FROM predictions WHERE model_version=?", (result["model_version"],)
            ).fetchall()
        self.assertEqual(len(rows), 130)  # every corpus segment scored
        unread_oof = [r["oof_score"] for r in rows if r["segment_id"].startswith("u")]
        self.assertTrue(all(v is None for v in unread_oof))  # not fitted on -> no out-of-fold score
        runs = model_runs.list_model_runs(self.theme)
        self.assertEqual(runs[0]["negative_mode"], "reviewed")

    def test_too_few_judged_negatives_skips(self):
        reviewed.set_item_review("d1", "neg", "s", False)
        result = classifier.run_training_pass(negative_mode="reviewed")
        self.assertEqual(result["results"][0]["status"], "skipped_insufficient_negatives")
        result = classifier.run_training_pass(negative_mode="all_uncoded")
        self.assertEqual(result["results"][0]["status"], "trained")
        self.assertEqual(result["results"][0]["n_neg"], 100)


if __name__ == "__main__":
    unittest.main()
