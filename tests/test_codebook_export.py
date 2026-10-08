"""Regression tests for the codebook export (docs/CODEBOOK_PLAN.md K7, codebook_export.py):
which (segment, theme) pairs get a label and from which source, that exemplars and near
misses are only researcher-flagged passages with twins collapsed, which themes are
exported, and that codebook_version is stable. Mistakes here don't raise errors; they
hand an external model wrong labels or a wrong "typical case". Covered under CLAUDE.md
rule 8's label-logic exception.

Every test uses a throwaway coding.db and export folder in a temp directory, never a real
project (CLAUDE.md rule 11), and restores schema.DB_PATH afterwards without init_db().

Run with: python -m unittest discover -s tests
"""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import codebook_export  # noqa: E402
import segmentation  # noqa: E402
from coding_store import activity_log, codes, examples, reviewed, schema, schemes, segments, themes  # noqa: E402

TWIN_TEXT = ("we doubled revenue this year and expanded into three new markets quickly while "
             "keeping our costs flat and hiring carefully across every single team we run")


def words(tag, n=25):
    """A distinct ~n-word turn (at least MERGE_MIN words, so each turn is its own segment)."""
    return " ".join(f"{tag}w{i}" for i in range(n))


def record(item_id, person, turns):
    return {"item_id": item_id, "group_name": f"G_{person}", "person_name": person,
            "turns": [{"speaker_name": spk, "speaker_role": role, "content": content}
                      for spk, role, content in turns]}


def build_records():
    """Alternating speakers so every non-empty turn becomes exactly one segment. i4 starts
    with an empty-content turn, so its segment 0000 comes from turn index 1."""
    recs = []
    for item in ("i1", "i2", "i3"):
        recs.append(record(item, f"P_{item}", [
            ("Exec" if k % 2 == 0 else "Host", "executive" if k % 2 == 0 else "interviewer", words(f"{item}t{k}"))
            for k in range(6)
        ]))
    recs.append(record("i4", "Twin", [("Host", "interviewer", ""), ("Exec", "executive", TWIN_TEXT),
                                      ("Host", "interviewer", words("i4t2"))]))
    recs.append(record("i5", "Twin", [("Exec", "executive", TWIN_TEXT), ("Host", "interviewer", words("i5t1"))]))
    return recs


class CodebookExportTests(unittest.TestCase):
    def setUp(self):
        self._orig_db = schema.DB_PATH
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / "exports"
        schema.set_db_path(Path(self._tmp.name) / "coding.db")
        self.records = build_records()
        segs = []
        for rec in self.records:
            for s in segmentation.segment_interview(rec):
                segs.append({**s, "dataset_id": "d1"})
        segments.upsert_segments(segs)

        schemes.create_scheme("S")
        self.p = themes.create_theme("Parent", scheme_id="s", description="the parent theme")["theme_id"]
        self.c = themes.create_theme("Child", scheme_id="s", parent_theme_id=self.p)["theme_id"]
        self.archived = themes.create_theme("Old", scheme_id="s")["theme_id"]
        self.merged = themes.create_theme("Dup", scheme_id="s")["theme_id"]
        themes.set_theme_status(self.archived, "archived")
        themes.merge_themes(self.merged, self.p)

        # i1: five coded segments (inferred_by_coverage), one only via the sub-code.
        codes.add_code("i1:0000", self.c)
        for k in range(1, 5):
            codes.add_code(f"i1:{k:04d}", self.p)
        # i2: one code, then an unconfirmed bulk mark -> its uncoded segments stay neutral,
        # except one confirmed "none of these".
        codes.add_code("i2:0000", self.p)
        reviewed.bulk_mark_items([("d1", "i2")], "s", "last_code")
        reviewed.set_segment_none("i2:0002", "s", True)
        # i3: read in full (interactive mark); one passage rejected and also "none of these":
        # the rejection wins. Another rejected and flagged a near miss.
        codes.add_negative("i3:0000", self.p)
        reviewed.set_segment_none("i3:0000", "s", True)
        codes.add_negative("i3:0001", self.p)
        examples.set_flag("i3:0001", self.p, "near_miss", True, note="looks like it")
        reviewed.set_item_review("d1", "i3", "s", True)
        # Twins: the same passage held as two records, both flagged; i4's flag is earlier.
        codes.add_code("i4:0000", self.p)
        codes.add_code("i5:0000", self.p)
        examples.set_flag("i4:0000", self.p, "exemplar", True, note="typical")
        examples.set_flag("i5:0000", self.p, "exemplar", True)

    def tearDown(self):
        schema.DB_PATH = self._orig_db  # restore without init_db() -- never touch the real file
        self._tmp.cleanup()

    def load_records(self, dataset_id):
        return [dict(r) for r in self.records] if dataset_id == "d1" else None

    def export(self):
        result = codebook_export.export_codebook_themes(self.out, "s", load_records=self.load_records, actor="tester")
        with open(self.out / codebook_export.JSON_NAME, encoding="utf-8") as f:
            doc = json.load(f)
        with open(self.out / codebook_export.CSV_NAME, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        return result, doc, rows

    def labels_for(self, rows, theme_id):
        return {r["segment_id"]: (r["label"], r["label_source"]) for r in rows if r["theme_id"] == theme_id}

    def topic(self, doc, theme_id):
        return next(t for t in doc["topics"] if t["topic_id"] == theme_id)

    def test_labels_and_sources(self):
        _, _, rows = self.export()
        self.assertEqual(list(rows[0].keys()), codebook_export.LABEL_FIELDS)
        parent = self.labels_for(rows, self.p)
        expected = {
            "i1:0000": ("pos", "code_rollup"),  # coded only with the sub-code
            **{f"i1:{k:04d}": ("pos", "code") for k in range(1, 5)},
            "i2:0000": ("pos", "code"),
            "i4:0000": ("pos", "code"), "i5:0000": ("pos", "code"),
            "i2:0002": ("neg", "none_confirmed"),
            "i3:0000": ("neg", "explicit_rejection"),  # also "none of these": the rejection wins
            "i3:0001": ("neg", "explicit_rejection"),  # an interviewer turn, but explicitly rejected
            # Uncoded turns of a read-in-full interview -- executive turns only: i3:0003 and
            # i3:0005 are interviewer turns, never offered for coding, so they stay neutral.
            "i3:0002": ("neg", "read_in_full_uncoded"),
            "i3:0004": ("neg", "read_in_full_uncoded"),
        }
        self.assertEqual(parent, expected)  # everything else (i1:0005, most of i2, ...) is neutral
        child = self.labels_for(rows, self.c)
        self.assertEqual(child["i1:0000"], ("pos", "code"))
        self.assertEqual(child["i3:0000"], ("neg", "explicit_rejection"))  # a parent's rejection flows down
        self.assertNotIn("i1:0001", child)  # a parent code says nothing about the child

    def test_unconfirmed_bulk_mark_gives_no_uncoded_negatives(self):
        _, _, rows = self.export()
        i2 = [r for r in rows if r["item_id"] == "i2" and r["label"] == "neg"]
        self.assertTrue(i2)
        self.assertTrue(all(r["label_source"] == "none_confirmed" for r in i2))
        self.assertTrue(all(r["item_read_in_full_status"] == "bulk_unconfirmed" for r in i2))
        statuses = {r["item_id"]: r["item_read_in_full_status"] for r in rows if r["theme_id"] == self.p}
        self.assertEqual((statuses["i1"], statuses["i3"]), ("none", "interactive"))
        coverage = {r["item_id"]: r["inferred_by_coverage"] for r in rows if r["theme_id"] == self.p}
        self.assertEqual((coverage["i1"], coverage["i2"]), ("true", "false"))

    def test_flagged_only_exemplars_and_twins_collapsed(self):
        _, doc, rows = self.export()
        parent = self.topic(doc, self.p)
        self.assertEqual([e["segment_id"] for e in parent["exemplars"]], ["i4:0000"])
        self.assertEqual(parent["exemplars"][0]["twin_segment_ids"], ["i5:0000"])
        self.assertEqual(parent["exemplars"][0]["note"], "typical")
        self.assertEqual([n["segment_id"] for n in parent["near_misses"]], ["i3:0001"])
        # The child has coded passages but nothing flagged: no fallback to anything else.
        child = self.topic(doc, self.c)
        self.assertEqual((child["exemplars"], child["near_misses"]), ([], []))
        self.assertFalse(child["completeness"]["exemplars"])
        by_seg = {r["segment_id"]: r for r in rows if r["theme_id"] == self.p}
        self.assertEqual((by_seg["i4:0000"]["twin_group"], by_seg["i5:0000"]["twin_group"]), ("i4:0000", "i4:0000"))
        self.assertEqual((by_seg["i5:0000"]["is_exemplar"], by_seg["i3:0001"]["is_near_miss"]), ("true", "true"))
        self.assertEqual((by_seg["i1:0001"]["is_exemplar"], by_seg["i1:0001"]["twin_group"]), ("false", ""))

    def test_merged_excluded_archived_included(self):
        _, doc, rows = self.export()
        ids = {t["topic_id"]: t["status"] for t in doc["topics"]}
        self.assertEqual(ids, {self.p: "active", self.c: "active", self.archived: "archived"})
        self.assertNotIn(self.merged, {r["theme_id"] for r in rows})
        self.assertEqual(self.topic(doc, self.c)["parent_topic_id"], self.p)

    def test_document_shape_and_log(self):
        result, doc, _ = self.export()
        self.assertEqual((doc["format"], doc["format_version"]), ("codebook_topic_themes", 1))
        self.assertEqual(doc["schema_version"], schema.CURRENT_SCHEMA_VERSION)
        self.assertEqual(doc["label_policy"]["read_in_full_counts"], ["confirmed", "interactive"])
        self.assertEqual(doc["label_policy"]["inferred_by_coverage_min_coded"], reviewed.READ_IN_FULL_MIN_CODED)
        counts = self.topic(doc, self.p)["counts"]
        self.assertEqual((counts["positives"], counts["explicit_rejections"], counts["reviewed_negatives"]),
                         (8, 2, 5))
        self.assertEqual({c["theme_id"] for c in result["completeness"]}, {self.p, self.c, self.archived})
        logged = activity_log.list_activity(action_type="codebook_themes_export")
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["details"]["codebook_version"], doc["codebook_version"])

    def test_codebook_version_stable_and_content_sensitive(self):
        _, first, _ = self.export()
        _, second, _ = self.export()
        self.assertTrue(first["codebook_version"].startswith("sha256:"))
        self.assertEqual(first["codebook_version"], second["codebook_version"])
        themes.update_theme(self.c, {"inclusion_criteria": "when the child applies"})
        _, third, _ = self.export()
        self.assertNotEqual(first["codebook_version"], third["codebook_version"])

    def test_coding_more_does_not_change_codebook_version(self):
        # Counts/completeness are derived from coding, not authored: excluded from the hash.
        _, before, rows = self.export()
        neg = next(r for r in rows if r["label"] == "neg")
        codes.add_code(neg["segment_id"], neg["theme_id"])
        _, after, _ = self.export()
        self.assertEqual(before["codebook_version"], after["codebook_version"])

    def test_uncoded_interviewer_turns_are_not_negatives(self):
        # The Code tab never offers interviewer turns for coding, so an uncoded one in a
        # read-in-full interview was never judged. (An explicit rejection still counts.)
        _, _, rows = self.export()
        self.assertTrue(any(r["label_source"] == "read_in_full_uncoded" for r in rows))
        self.assertFalse([r for r in rows if r["label_source"] == "read_in_full_uncoded"
                          and r["speaker_role"] == "interviewer"])

    def test_turn_indices(self):
        if getattr(segmentation, "segment_turn_indices", None) is None:
            self.skipTest("segmentation.segment_turn_indices (K6) isn't available")
        _, doc, rows = self.export()
        exemplar = self.topic(doc, self.p)["exemplars"][0]
        self.assertEqual(exemplar["turn_indices"], [1])  # i4's turn 0 is empty
        by_seg = {r["segment_id"]: r for r in rows if r["theme_id"] == self.p}
        self.assertEqual(by_seg["i3:0002"]["turn_indices"], "2")
        result = codebook_export.export_codebook_themes(self.out, "s", load_records=None)
        self.assertFalse(result["turn_indices_available"])


if __name__ == "__main__":
    unittest.main()
