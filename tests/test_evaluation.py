"""Regression tests for Review -> Evaluate (coding_store/evaluation.py, CODEBOOK_PLAN U6):
which passages count as gold, how segment labels and scores roll up to turns, and the
metrics reported on them. A mistake here doesn't raise an error; it quietly reports a
precision or recall the researcher might cite -- the same reason as CLAUDE.md rule 8's
label-building exception.

Every database test uses a throwaway coding.db in a temp directory, never a real project
(CLAUDE.md rule 11), and restores schema.DB_PATH afterwards.

Run with: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from coding_store import codes, evaluation, model_runs, reviewed, schema, schemes, segments, themes  # noqa: E402


class PureFunctionTests(unittest.TestCase):
    def test_wilson_matches_known_interval(self):
        self.assertEqual(evaluation.wilson(8, 10), [0.49, 0.943])
        self.assertIsNone(evaluation.wilson(0, 0))

    def test_prf_leaves_undefined_ratios_empty(self):
        m = evaluation.prf([False, False], [False, True])
        self.assertIsNone(m["precision"])  # nothing predicted: precision undefined, not 0
        self.assertEqual(m["recall"], 0)
        self.assertIsNone(m["f1"])

    def test_prf_counts(self):
        m = evaluation.prf([True, True, False, False], [True, False, True, False])
        self.assertEqual((m["tp"], m["fp"], m["fn"]), (1, 1, 1))
        self.assertEqual(m["precision"], 0.5)
        self.assertEqual(m["f1"], 0.5)

    def test_average_precision_matches_sklearn_definition(self):
        # positives ranked 1st and 3rd: AP = (1/1 + 2/3) / 2
        self.assertAlmostEqual(evaluation.average_precision([0.9, 0.8, 0.7, 0.1], [1, 0, 1, 0]), (1 + 2 / 3) / 2)
        # tied scores are taken together, as sklearn does
        self.assertAlmostEqual(evaluation.average_precision([0.5, 0.5], [1, 0]), 0.5)
        self.assertIsNone(evaluation.average_precision([0.5], [0]))

    def test_turn_is_positive_if_any_segment_is_and_takes_the_highest_score(self):
        seg = lambda sid, turns, pos, score: {"segment_id": sid, "dataset_id": "d", "item_id": "1",
                                              "turn_indices": turns, "positive": pos, "score": score}
        units = evaluation.roll_up_to_turns([
            seg("a", [0], False, 0.2), seg("b", [0], True, 0.6),  # one long turn split in two
            seg("c", [1, 2], False, 0.3),                         # two short turns merged
            seg("d", [], True, 0.9),                              # record not loadable
        ])
        by_key = {u["key"]: u for u in units}
        self.assertEqual(len(units), 4)
        self.assertTrue(by_key[("turn", "d", "1", 0)]["positive"])
        self.assertEqual(by_key[("turn", "d", "1", 0)]["score"], 0.6)
        self.assertFalse(by_key[("turn", "d", "1", 2)]["positive"])
        self.assertTrue(by_key[("segment", "d")]["by_segment"])

    def test_calibration_last_band_includes_one(self):
        bands = evaluation.calibration_bands([1.0, 0.1, 0.15], [True, False, True])
        self.assertEqual(bands[-1]["n"], 1)
        self.assertEqual(bands[0]["share_positive"], 0.5)


class FakeTurns:
    """segment_id -> turn indices, standing in for codebook_export.TurnIndexLookup."""

    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, segment_id, dataset_id, item_id):
        return self.mapping.get(segment_id, [])


class EvaluateRunTests(unittest.TestCase):
    def setUp(self):
        self._orig_db = schema.DB_PATH
        self._tmp = tempfile.TemporaryDirectory()
        schema.set_db_path(Path(self._tmp.name) / "coding.db")
        self.scheme = schemes.create_scheme("S")["scheme_id"]
        self.theme = themes.create_theme("Growth", scheme_id=self.scheme)["theme_id"]
        segs = []
        for item, n in (("i1", 3), ("i2", 2)):
            for k in range(n):
                segs.append({"segment_id": f"{item}_{k}", "item_id": item, "dataset_id": "d", "seg_index": k,
                             "text": "words " * 10, "word_count": 10})
        segments.upsert_segments(segs)
        codes.add_code("i1_0", self.theme)
        codes.add_code("i2_0", self.theme)
        reviewed.set_item_review("d", "i1", self.scheme, True)  # interactive: read in full
        reviewed.bulk_mark_items([("d", "i2")], self.scheme, "last_code")  # unconfirmed: not gold

    def tearDown(self):
        schema.DB_PATH = self._orig_db  # restore without init_db() -- never touch the real file
        self._tmp.cleanup()

    def save_run(self, version, theme_id, scores, oof=None, threshold=0.5):
        model_runs.save_model_run(version, theme_id, "2026-10-09T00:00:00+00:00",
                                  {"n_pos": 1, "n_neg": 2, "precision": None, "recall": None, "f1": None,
                                   "pr_auc": None, "threshold": threshold})
        ids = sorted(scores)
        model_runs.save_predictions(version, theme_id, ids, [scores[i] for i in ids],
                                    [(oof or {}).get(i) for i in ids])

    def test_gold_is_read_in_full_interviews_only_rolled_up_to_turns(self):
        # i1_0 and i1_1 are one turn; i1_2 another. Unconfirmed i2 must not count.
        self.save_run("tfidf_v1_a", self.theme, {"i1_0": 0.2, "i1_1": 0.9, "i1_2": 0.1, "i2_0": 0.9, "i2_1": 0.9})
        result = evaluation.evaluate_run(self.scheme, "tfidf_v1_a",
                                         turn_lookup=FakeTurns({"i1_0": [0], "i1_1": [0], "i1_2": [1]}))
        t = result["themes"][0]
        self.assertEqual(t["gold_interviews"], 1)
        self.assertEqual((t["n_units"], t["n_pos"]), (2, 1))
        self.assertEqual((t["run"]["tp"], t["run"]["fp"], t["run"]["fn"]), (1, 0, 0))
        self.assertEqual(result["gold"]["unconfirmed_marks"], 1)

    def test_out_of_fold_score_is_used_when_the_run_fitted_on_the_passage(self):
        # the full-fit score memorized the label (0.95); out of fold, the model missed it
        self.save_run("tfidf_v1_a", self.theme, {"i1_0": 0.95, "i1_1": 0.1, "i1_2": 0.1}, oof={"i1_0": 0.3})
        t = evaluation.evaluate_run(self.scheme, "tfidf_v1_a")["themes"][0]
        self.assertEqual(t["run"]["fn"], 1)
        self.assertEqual(t["run"]["tp"], 0)

    def test_a_mark_made_before_the_theme_existed_is_not_gold_for_it(self):
        later = themes.create_theme("Later", scheme_id=self.scheme)["theme_id"]
        self.save_run("tfidf_v1_a", later, {"i1_0": 0.9, "i1_1": 0.1, "i1_2": 0.1})
        t = evaluation.evaluate_run(self.scheme, "tfidf_v1_a")["themes"][0]
        self.assertEqual(t["note"], "no_gold")
        self.assertIsNone(t["run"])

    def test_comparison_run_is_scored_on_the_same_passages(self):
        self.save_run("tfidf_v1_a", self.theme, {"i1_0": 0.9, "i1_1": 0.1, "i1_2": 0.1})
        self.save_run("embed_v1_b", self.theme, {"i1_0": 0.1, "i1_1": 0.1, "i1_2": 0.9})
        t = evaluation.evaluate_run(self.scheme, "tfidf_v1_a", "embed_v1_b")["themes"][0]
        self.assertEqual(t["run"]["f1"], 1)
        self.assertEqual((t["compare"]["tp"], t["compare"]["fp"]), (0, 1))
        self.assertEqual(t["compare"]["n_units"], 3)

    def test_pruned_scores_are_reported_not_evaluated(self):
        self.save_run("tfidf_v1_a", self.theme, {})
        t = evaluation.evaluate_run(self.scheme, "tfidf_v1_a")["themes"][0]
        self.assertEqual(t["note"], "scores_pruned")

    def test_a_run_of_another_scheme_is_refused(self):
        with self.assertRaises(ValueError):
            evaluation.evaluate_run(self.scheme, "tfidf_v1_missing")


if __name__ == "__main__":
    unittest.main()
