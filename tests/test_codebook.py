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

from coding_store import codebook, codes, reviewed, schema, schemes, segments, themes  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
