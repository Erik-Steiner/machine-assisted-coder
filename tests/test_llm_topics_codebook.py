"""Regression tests for the LLM pipeline's reading of the app's codebook export
(docs/CODEBOOK_PLAN.md K8, scripts/llm_topics/stage0_prepare.py): format detection, the
field mapping, the segment -> turn comment_id conversion, which topics are coded, and the
gold tier's labels. Mistakes here don't raise errors; they hand the LLM a wrong example
turn or grade it against wrong labels. Covered under CLAUDE.md rule 8's narrow exception:
these are pure functions (no coding.db, no server, no file I/O), cheap to test.

Run with: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(HERE / "scripts" / "llm_topics"))

# scripts/llm_topics/ and scripts/export_interview_turns.py are in the repo, so this normally
# imports; the skip only applies if they've been removed or can't be imported.
try:
    import export_interview_turns  # noqa: E402
    import stage0_prepare as s0  # noqa: E402
except ImportError as e:
    raise unittest.SkipTest(f"LLM pipeline scripts not present: {e}")


class FormatDetectionTests(unittest.TestCase):
    def test_app_export(self):
        self.assertEqual(s0.codebook_format({"format": "codebook_topic_themes", "format_version": 1, "topics": []}),
                         s0.APP_FORMAT)

    def test_hand_edited_codebook_has_no_format_key(self):
        self.assertEqual(s0.codebook_format({"codebook_version": "seed-0", "topics": []}), s0.SEED_FORMAT)

    def test_unknown_format_version_fails_loudly(self):
        with self.assertRaises(ValueError):
            s0.codebook_format({"format": "codebook_topic_themes", "format_version": 99})


class FieldMappingTests(unittest.TestCase):
    def test_app_fields_map_to_pipeline_names(self):
        t = {"definition": "D", "inclusion_criteria": "I", "exclusion_criteria": "E", "boundary_notes": None}
        self.assertEqual(s0.app_topic_fields(t), {"description": "D", "include": "I", "exclude": "E",
                                                  "boundary_notes": ""})

    def test_archived_topics_are_left_out(self):
        cb = {"topics": [{"topic_id": "a", "status": "active"}, {"topic_id": "b", "status": "archived"},
                         {"topic_id": "c", "status": "active", "parent_topic_id": "a"}]}
        active, left_out = s0.app_themes(cb)
        self.assertEqual(active, ["a", "c"])
        self.assertEqual([t["topic_id"] for t in left_out], ["b"])


class CommentIdTests(unittest.TestCase):
    def test_matches_the_turn_export_numbering(self):
        # an empty turn in the middle must not shift the numbering (K6 check)
        rec = {"item_id": 4242, "person_name": "Exec", "turns": [
            {"speaker_name": "Host", "content": "Question?"},
            {"speaker_name": "Exec", "content": ""},
            {"speaker_name": "Exec", "content": "Answer."},
        ]}
        rows = export_interview_turns.interview_rows(rec, "Co", "f", "f", {"Exec"}, [])
        self.assertEqual([r["comment_id"] for r in rows],
                         [s0.turn_comment_id(rec["item_id"], i) for i in range(len(rec["turns"]))])
        self.assertEqual(s0.turn_comment_id("442850", 0), "442850_0001")


class GoldTierTests(unittest.TestCase):
    """Two interviews: 1 marked read in full in the app (its negatives are in the file),
    2 only inferred_by_coverage (its uncoded segments are absent from the file)."""

    def setUp(self):
        self.tmap = pd.DataFrame({"interview_id": [1, 1, 2, 2], "turn_id": [1, 2, 1, 2],
                                  "segment_id": ["1:0000", "1:0001", "2:0000", "2:0001"],
                                  "segment_text": ["a", "b", "c", "d"]})
        self.turns = self.tmap[["interview_id", "turn_id"]]
        row = lambda sid, item, label, src, status, cov: {
            "segment_id": sid, "item_id": item, "theme_id": "t", "label": label, "label_source": src,
            "item_read_in_full_status": status, "inferred_by_coverage": cov, "person_name": ""}
        self.labels = pd.DataFrame([
            row("1:0000", "1", "pos", "code", "interactive", "true"),
            row("1:0001", "1", "neg", "read_in_full_uncoded", "interactive", "true"),
            row("2:0000", "2", "pos", "code", "bulk_unconfirmed", "true"),
            row("9:0000", "9", "pos", "code", "none", "false"),  # not in the turn exports
        ])
        self.scores = pd.DataFrame(columns=["segment_id", "theme_id", "score", "oof_score"])

    def run_tier(self, tier):
        return s0.app_segment_labels(self.labels, self.scores, ["t"], self.tmap, self.turns, tier,
                                     s0.APP_READ_IN_FULL_STATUSES)

    def test_status_tier_is_the_apps_policy(self):
        lab, rif, pos_only, stats = self.run_tier("status")
        self.assertEqual(rif, {1})
        self.assertEqual(pos_only, {2})
        self.assertNotIn("2:0001", set(lab["segment_id"]))  # neutral: absent, never a negative
        self.assertEqual(stats["rows_unmatched"], 1)

    def test_coverage_tier_adds_uncoded_negatives(self):
        lab, rif, pos_only, stats = self.run_tier("coverage")
        self.assertEqual(rif, {1, 2})
        self.assertEqual(pos_only, set())
        got = lab.set_index("segment_id")["label"].to_dict()
        self.assertEqual(got["2:0001"], "neg")
        self.assertEqual(got["2:0000"], "pos")
        self.assertEqual(stats["coverage_negatives_added"], 1)


if __name__ == "__main__":
    unittest.main()
