"""Regression tests for codebook rigor (docs/CODEBOOK_PLAN.md K1/K2): the completeness
rule, which "fully coded" marks count as read in full, and the flag consistency rule
(an exemplar / near-miss flag lives only on an active row). Mistakes here don't raise
errors; they mislead the researcher about how ready a codebook is, or let a stale flag
leak into an export. Covered under CLAUDE.md rule 8's label-logic exception. Each test
uses a throwaway coding.db (rule 11).

Run with: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import classifier  # noqa: E402
from coding_store import codebook, codes, decisions, examples, labels, reviewed, schema, schemes, segments, themes  # noqa: E402


class CompletenessRuleTests(unittest.TestCase):
    FULL = {"exemplars": 3, "exemplar_groups": 2, "near_misses": 2, "read_in_full_items_with_positive": 1}

    def theme(self, **kw):
        base = {"description": " ".join(["word"] * 15), "inclusion_criteria": "x", "exclusion_criteria": "y"}
        return {**base, **kw}

    def test_all_pass(self):
        c = codebook.theme_completeness(self.theme(), self.FULL)
        self.assertEqual((c["score"], c["total"]), (5, 5))

    def test_each_threshold(self):
        c = codebook.theme_completeness(self.theme(description="too short"), self.FULL)
        self.assertFalse(c["definition"]["ok"])
        c = codebook.theme_completeness(self.theme(exclusion_criteria="  "), self.FULL)
        self.assertEqual((c["criteria"]["ok"], c["criteria"]["detail"]), (False, "missing exclusion"))
        c = codebook.theme_completeness(self.theme(), {**self.FULL, "exemplar_groups": 1})
        self.assertFalse(c["exemplars"]["ok"])  # three exemplars, but all from one group
        c = codebook.theme_completeness(self.theme(), {**self.FULL, "near_misses": 1})
        self.assertFalse(c["near_misses"]["ok"])
        c = codebook.theme_completeness(self.theme(), {**self.FULL, "read_in_full_items_with_positive": 0})
        self.assertEqual((c["gold"]["ok"], c["score"]), (False, 4))


class CodebookDbTests(unittest.TestCase):
    def setUp(self):
        self._orig_db = schema.DB_PATH
        self._tmp = tempfile.TemporaryDirectory()
        schema.set_db_path(Path(self._tmp.name) / "coding.db")
        schemes.create_scheme("S")
        self.t = themes.create_theme("T", scheme_id="s", inclusion_criteria="when x")["theme_id"]
        segments.upsert_segments([
            {"segment_id": f"{item}:{i}", "item_id": item, "dataset_id": "d1", "seg_index": i,
             "text": "t", "word_count": 5, "group_name": group}
            for item, group in (("i1", "G1"), ("i2", "G2")) for i in range(2)
        ])

    def tearDown(self):
        schema.DB_PATH = self._orig_db  # restore without init_db() -- never touch the real file
        self._tmp.cleanup()

    def test_create_and_update_criteria(self):
        self.assertEqual(themes.get_theme(self.t)["inclusion_criteria"], "when x")
        themes.update_theme(self.t, {"exclusion_criteria": "not y", "boundary_notes": "vs Z"})
        t = themes.get_theme(self.t)
        self.assertEqual((t["exclusion_criteria"], t["boundary_notes"]), ("not y", "vs Z"))

    def test_read_in_full_excludes_bulk_until_confirmed(self):
        codes.add_code("i1:0", self.t)
        codes.add_code("i2:0", self.t)
        reviewed.set_item_review("d1", "i1", "s", True)  # interactive: counts
        reviewed.bulk_mark_items([("d1", "i2")], "s", "last_code")  # back-dated bulk: doesn't yet
        self.assertEqual(codebook.read_in_full_items("s"), {("d1", "i1")})
        with schema.get_conn() as conn:
            conn.execute("UPDATE item_reviews SET read_in_full_confirmed_at=? WHERE item_id='i2'", (schema._now(),))
        self.assertEqual(codebook.read_in_full_items("s"), {("d1", "i1"), ("d1", "i2")})
        self.assertEqual(codebook.theme_counts(self.t)["read_in_full_items_with_positive"], 2)

    def test_counts_only_active_flags_and_reapplied_rows_lose_their_flag(self):
        codes.add_code("i1:0", self.t)
        codes.add_code("i2:0", self.t)
        codes.add_negative("i1:1", self.t)
        with schema.get_conn() as conn:
            conn.execute("UPDATE codes SET is_exemplar=1")
            conn.execute("UPDATE negatives SET is_near_miss=1")
        counts = codebook.theme_counts(self.t)
        self.assertEqual((counts["exemplars"], counts["exemplar_groups"], counts["near_misses"]), (2, 2, 1))
        codes.remove_code("i1:0", self.t)  # soft-deleted: its flag no longer counts
        self.assertEqual(codebook.theme_counts(self.t)["exemplars"], 1)
        codes.add_code("i1:0", self.t)  # re-applied: a fresh decision, no flag
        self.assertEqual(codebook.theme_counts(self.t)["exemplars"], 1)
        codes.remove_negative("i1:1", self.t)
        codes.add_negative("i1:1", self.t)
        self.assertEqual(codebook.theme_counts(self.t)["near_misses"], 0)

    def test_overview(self):
        rows = codebook.overview("s")
        self.assertEqual([r["theme_id"] for r in rows], [self.t])
        self.assertEqual(rows[0]["completeness"]["total"], 5)


class ReadInFullTests(unittest.TestCase):
    """coding_store/reviewed.py's read-in-full statuses and the "read_in_full" negative
    mode (CODEBOOK_PLAN K5)."""

    def setUp(self):
        self._orig_db = schema.DB_PATH
        self._tmp = tempfile.TemporaryDirectory()
        schema.set_db_path(Path(self._tmp.name) / "coding.db")
        schemes.create_scheme("S")
        self.t = themes.create_theme("T", scheme_id="s")["theme_id"]
        segments.upsert_segments([
            {"segment_id": f"{item}:{i}", "item_id": item, "dataset_id": "d1", "seg_index": i,
             "text": "t", "word_count": 5}
            for item in ("i1", "i2", "i3") for i in range(6)
        ])
        for i in range(5):
            codes.add_code(f"i1:{i}", self.t)  # i1: 5 coded -> inferred_by_coverage
        codes.add_code("i2:0", self.t)        # i2: 1 coded

    def tearDown(self):
        schema.DB_PATH = self._orig_db  # restore without init_db() -- never touch the real file
        self._tmp.cleanup()

    def test_statuses(self):
        self.assertEqual(reviewed.read_in_full_status("d1", "i1", "s"), "none")
        reviewed.bulk_mark_items([("d1", "i1"), ("d1", "i2")], "s", "last_code")
        reviewed.set_item_review("d1", "i3", "s", True)
        self.assertEqual([reviewed.read_in_full_status("d1", i, "s") for i in ("i1", "i2", "i3")],
                         ["bulk_unconfirmed", "bulk_unconfirmed", "interactive"])
        with schema.get_conn() as conn:  # a pre-v6 mark has no recorded_at
            conn.execute("UPDATE item_reviews SET recorded_at=NULL WHERE item_id='i3'")
        self.assertEqual(reviewed.read_in_full_status("d1", "i3", "s"), "unknown")

    def test_confirm_keeps_marked_at_and_unconfirmed_list(self):
        reviewed.bulk_mark_items([("d1", "i1"), ("d1", "i2")], "s", "last_code")
        listed = {(m["item_id"], m["coded_segments"], m["inferred_by_coverage"]) for m in reviewed.unconfirmed_marks("s")}
        self.assertEqual(listed, {("i1", 5, True), ("i2", 1, False)})
        before = reviewed.get_item_review("d1", "i2", "s")["marked_at"]
        mark = reviewed.set_read_in_full_confirmed("d1", "i2", "s", True)
        self.assertEqual((reviewed.mark_status(mark), mark["marked_at"]), ("confirmed", before))
        self.assertEqual([m["item_id"] for m in reviewed.unconfirmed_marks("s")], ["i1"])
        self.assertEqual(reviewed.read_in_full_items("s"), {("d1", "i2")})
        with self.assertRaises(ValueError):
            reviewed.set_read_in_full_confirmed("d1", "i3", "s", True)  # not marked at all

    def test_read_in_full_negative_mode(self):
        reviewed.bulk_mark_items([("d1", "i1"), ("d1", "i2")], "s", "last_code")
        reviewed.set_item_review("d1", "i3", "s", True)
        reviewed.set_segment_none("i2:5", "s", True)
        all_marks = labels.reviewed_negative_segment_ids(self.t)
        strict = labels.reviewed_negative_segment_ids(self.t, read_in_full_only=True)
        self.assertEqual(all_marks, {"i1:5", *(f"i2:{i}" for i in range(1, 6)), *(f"i3:{i}" for i in range(6))})
        # Only the interactive mark (i3) plus the explicit "none of these" on i2:5.
        self.assertEqual(strict, {"i2:5", *(f"i3:{i}" for i in range(6))})
        ids = [f"{item}:{i}" for item in ("i1", "i2", "i3") for i in range(6)]
        _, _, n_pos, n_neg = classifier.training_labels(self.t, ids, "read_in_full")
        self.assertEqual((n_pos, n_neg), (6, 7))


class ExampleFlagTests(unittest.TestCase):
    """coding_store/examples.py and the flagged triage decisions (CODEBOOK_PLAN K3)."""

    CTX = {"mode": "top", "engine": "tfidf", "model_version": "tfidf_v1_1", "queue_theme_id": "t"}

    def setUp(self):
        self._orig_db = schema.DB_PATH
        self._tmp = tempfile.TemporaryDirectory()
        schema.set_db_path(Path(self._tmp.name) / "coding.db")
        text = "we doubled revenue this year and expanded into three new markets quickly"
        segments.upsert_segments([
            {"segment_id": "a", "item_id": "i1", "dataset_id": "d1", "seg_index": 0, "text": text,
             "person_name": "P"},
            {"segment_id": "a_twin", "item_id": "i2", "dataset_id": "d1", "seg_index": 0,
             "text": text + " too", "person_name": "P"},
            {"segment_id": "b", "item_id": "i3", "dataset_id": "d1", "seg_index": 0,
             "text": "an unrelated remark about the weather", "person_name": "P"},
        ])

    def tearDown(self):
        schema.DB_PATH = self._orig_db  # restore without init_db() -- never touch the real file
        self._tmp.cleanup()

    def flagged(self, kind):
        return [r["segment_id"] for r in examples.list_flagged("t", kind)]

    def test_flag_needs_an_active_decision(self):
        with self.assertRaises(ValueError):
            examples.set_flag("a", "t", "exemplar", True)  # not coded
        codes.add_code("a", "t")
        examples.set_flag("a", "t", "exemplar", True, note="clear case")
        self.assertEqual(self.flagged("exemplar"), ["a"])
        self.assertEqual(examples.list_flagged("t", "exemplar")[0]["note"], "clear case")
        with self.assertRaises(ValueError):
            examples.set_flag("a", "t", "near_miss", True)  # coded, not rejected
        examples.set_flag("a", "t", "exemplar", False)
        self.assertEqual(self.flagged("exemplar"), [])

    def test_twin_warning_does_not_block(self):
        codes.add_code("a", "t")
        codes.add_code("a_twin", "t")
        codes.add_code("b", "t")
        examples.set_flag("a", "t", "exemplar", True)
        result = examples.set_flag("a_twin", "t", "exemplar", True)
        self.assertEqual([t["segment_id"] for t in result["twins"]], ["a"])
        self.assertEqual(examples.set_flag("b", "t", "exemplar", True)["twins"], [])
        self.assertEqual(sorted(self.flagged("exemplar")), ["a", "a_twin", "b"])  # warned, not blocked
        annotated = {r["segment_id"]: r["twin_of"] for r in examples.annotate_twins(examples.list_flagged("t", "exemplar"))}
        self.assertEqual(annotated, {"a": ["a_twin"], "a_twin": ["a"], "b": []})

    def test_flagged_decisions_count_as_accept_reject_and_undo_restores_flags(self):
        d = decisions.record_decision("a", "t", "accept", flag="exemplar", **self.CTX)
        decisions.record_decision("b", "t", "reject", flag="near_miss", **self.CTX)
        self.assertEqual((self.flagged("exemplar"), self.flagged("near_miss")), (["a"], ["b"]))
        self.assertEqual(decisions.round_summary("tfidf_v1_1", "t", "top")["accept"], 1)
        decisions.undo(d["decision_id"])
        self.assertEqual(self.flagged("exemplar"), [])  # the code it created is gone
        with self.assertRaises(ValueError):
            decisions.record_decision("a", "t", "reject", flag="exemplar", **self.CTX)

    def test_undo_puts_back_an_earlier_flag(self):
        codes.add_code("a", "t")
        examples.set_flag("a", "t", "exemplar", True, note="original note")
        d = decisions.record_decision("a", "t", "reject", **self.CTX)  # unflags by removing the code
        self.assertEqual(self.flagged("exemplar"), [])
        decisions.undo(d["decision_id"])
        rows = examples.list_flagged("t", "exemplar")
        self.assertEqual([(r["segment_id"], r["note"]) for r in rows], [("a", "original note")])


if __name__ == "__main__":
    unittest.main()
