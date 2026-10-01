"""Regression tests for coding_store/storage.py's pruning rule: which training
runs' per-segment scores may be deleted. A mistake here deletes research
scores that can't be regenerated exactly, and nothing in the UI would show it,
which is why this gets a narrow test net despite CLAUDE.md's "no automated test
suite" rule (see rule 8's named exceptions).

plan_prune() is pure (plain dicts in, sets out). The PruneEndToEnd tests use a
throwaway coding.db in a temp directory, never a real project (CLAUDE.md
rule 11), and restore schema.DB_PATH afterwards.

Run with: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from coding_store import codes, model_runs, schema, storage  # noqa: E402


def run(mv, theme, trained_at, engine=None):
    return {"model_version": mv, "theme_id": theme, "trained_at": trained_at,
            "engine": engine or model_runs.engine_of_version(mv)}


class PlanPruneTests(unittest.TestCase):
    def test_keeps_latest_n_per_theme(self):
        runs = [run(f"tfidf_v1_{i}", "a", f"2026-01-0{i}") for i in range(1, 5)]
        prunable, kept = storage.plan_prune(runs, set(), keep_latest=2)
        self.assertEqual(prunable, {("tfidf_v1_1", "a"), ("tfidf_v1_2", "a")})
        self.assertEqual(kept[("tfidf_v1_4", "a")], "latest")
        self.assertEqual(kept[("tfidf_v1_3", "a")], "recent")

    def test_latest_always_kept_even_with_keep_latest_1(self):
        runs = [run("tfidf_v1_1", "a", "2026-01-01"), run("tfidf_v1_2", "a", "2026-01-02")]
        prunable, kept = storage.plan_prune(runs, set(), keep_latest=1)
        self.assertEqual(prunable, {("tfidf_v1_1", "a")})
        self.assertEqual(kept[("tfidf_v1_2", "a")], "latest")

    def test_keep_latest_below_one_rejected(self):
        with self.assertRaises(ValueError):
            storage.plan_prune([], set(), keep_latest=0)

    def test_engines_are_kept_independently(self):
        # Newer embedding runs must never push the latest TF-IDF run out, or vice versa.
        runs = [
            run("tfidf_v1_1", "a", "2026-01-01"),
            run("embed_v1_2", "a", "2026-01-02"),
            run("embed_v1_3", "a", "2026-01-03"),
        ]
        prunable, kept = storage.plan_prune(runs, set(), keep_latest=1)
        self.assertEqual(kept[("tfidf_v1_1", "a")], "latest")
        self.assertEqual(kept[("embed_v1_3", "a")], "latest")
        self.assertEqual(prunable, {("embed_v1_2", "a")})

    def test_themes_are_ranked_independently(self):
        # A pass can be latest for one theme and old for another (theme b retrained later).
        runs = [
            run("tfidf_v1_1", "a", "2026-01-01"), run("tfidf_v1_1", "b", "2026-01-01"),
            run("tfidf_v1_2", "b", "2026-01-02"),
        ]
        prunable, kept = storage.plan_prune(runs, set(), keep_latest=1)
        self.assertEqual(kept[("tfidf_v1_1", "a")], "latest")
        self.assertEqual(prunable, {("tfidf_v1_1", "b")})

    def test_pinned_never_prunable(self):
        runs = [run(f"tfidf_v1_{i}", "a", f"2026-01-0{i}") for i in range(1, 4)]
        prunable, kept = storage.plan_prune(runs, {"tfidf_v1_1"}, keep_latest=1)
        self.assertEqual(kept[("tfidf_v1_1", "a")], "pinned")
        self.assertEqual(prunable, {("tfidf_v1_2", "a")})

    def test_unknown_engine_gets_its_own_group(self):
        runs = [run("tfidf_v1_1", "a", "2026-01-01"), run("ext_v1_x_2", "a", "2026-01-02", engine=None)]
        prunable, kept = storage.plan_prune(runs, set(), keep_latest=1)
        self.assertEqual(prunable, set())
        self.assertEqual(kept[("tfidf_v1_1", "a")], "latest")
        self.assertEqual(kept[("ext_v1_x_2", "a")], "latest")

    def test_trained_at_tie_is_deterministic(self):
        runs = [run("tfidf_v1_a", "t", "2026-01-01"), run("tfidf_v1_b", "t", "2026-01-01")]
        first = storage.plan_prune(runs, set(), keep_latest=1)
        second = storage.plan_prune(list(reversed(runs)), set(), keep_latest=1)
        self.assertEqual(first, second)


class PruneEndToEndTests(unittest.TestCase):
    def setUp(self):
        self._orig_db = schema.DB_PATH
        self._tmp = tempfile.TemporaryDirectory()
        schema.set_db_path(Path(self._tmp.name) / "coding.db")
        segs = ["s1", "s2", "s3"]
        for i, mv in enumerate(["tfidf_v1_1", "tfidf_v1_2", "tfidf_v1_3"], start=1):
            model_runs.save_model_run(mv, "a", f"2026-01-0{i}", {
                "n_pos": 1, "n_neg": 2, "precision": 1, "recall": 1, "f1": 1, "pr_auc": 1, "threshold": 0.5,
            })
            model_runs.save_predictions(mv, "a", segs, [0.1, 0.2, 0.3])
        codes.add_code("s1", "a")

    def tearDown(self):
        schema.DB_PATH = self._orig_db  # restore without init_db() -- never touch the real file
        self._tmp.cleanup()

    def _count(self, mv):
        with schema.get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM predictions WHERE model_version=?", (mv,)).fetchone()[0]

    def test_protected_pair_refuses_whole_request(self):
        with self.assertRaises(ValueError):
            storage.prune_predictions([("tfidf_v1_1", "a"), ("tfidf_v1_3", "a")], keep_latest=1)
        self.assertEqual(self._count("tfidf_v1_1"), 3)  # nothing deleted, not even the prunable pair

    def test_prunes_scores_only(self):
        result = storage.prune_predictions([("tfidf_v1_1", "a")], keep_latest=2)
        self.assertEqual(result["rows_deleted"], 3)
        self.assertEqual(self._count("tfidf_v1_1"), 0)
        self.assertEqual(self._count("tfidf_v1_2"), 3)
        self.assertEqual(len(model_runs.list_model_runs("a")), 3)  # run summaries kept
        self.assertEqual(codes.get_codes_for_segment_ids(["s1"]), {"s1": ["a"]})  # codes untouched

    def test_pin_protects_at_delete_time(self):
        storage.set_pin("tfidf_v1_1", True, note="cited")
        with self.assertRaises(ValueError):
            storage.prune_predictions([("tfidf_v1_1", "a")], keep_latest=1)
        self.assertEqual(self._count("tfidf_v1_1"), 3)

    def test_report_marks_pruned(self):
        storage.prune_predictions([("tfidf_v1_1", "a")], keep_latest=2)
        report = storage.storage_report(keep_latest=2)
        statuses = {p["model_version"]: p["themes"][0]["status"] for p in report["passes"]}
        self.assertEqual(statuses, {"tfidf_v1_1": "pruned", "tfidf_v1_2": "recent", "tfidf_v1_3": "latest"})


if __name__ == "__main__":
    unittest.main()
