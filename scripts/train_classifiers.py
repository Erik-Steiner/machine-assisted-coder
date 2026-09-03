"""Train the baseline classifier: one TF-IDF + LogisticRegression model per
codebook theme with enough labeled segments (see
classifier.MIN_POSITIVES_ATTEMPT), scored against the whole corpus.

Safe to re-run any time as coding progresses -- each run gets its own
model_version, so past model_runs stay in coding.db for comparison, and a
theme's predictions/top terms are simply superseded (not deleted) by its
newest run. This is a thin CLI wrapper around classifier.run_training_pass(),
the same function the webapp's Model tab calls for a web-triggered run.

By default, excludes non-canonical near-duplicate segments from the corpus
(e.g. the same speech re-transcribed by several outlets) once
scripts/find_duplicates.py has been run at least once -- see
classifier.build_corpus()'s docstring. Pass --include-duplicates to opt out
for a single run.

Usage: python scripts/train_classifiers.py
       python scripts/train_classifiers.py --include-duplicates
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import classifier


def print_progress(event):
    stage = event["stage"]
    if stage == "corpus":
        print("Building shared TF-IDF features over the full corpus...")
    elif stage == "vectorizing":
        print(f"{event['n_segments']} segments, fitting vectorizer...")
    elif stage == "theme_skipped":
        print(f"  skip {event['name']}: {event['n_pos']}/{classifier.MIN_POSITIVES_ATTEMPT} positives needed")
    elif stage == "theme_done":
        m = event["metrics"]
        caution = (
            "" if m["n_pos"] >= classifier.TRUST_POSITIVES
            else f"  [below the {classifier.TRUST_POSITIVES}-positive rule of thumb -- treat ranking with caution]"
        )
        print(
            f"  {event['name']}: n_pos={m['n_pos']} "
            f"precision={m['precision']:.2f} recall={m['recall']:.2f} "
            f"f1={m['f1']:.2f} pr_auc={m['pr_auc']:.2f}{caution}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--include-duplicates", action="store_true",
                         help="don't exclude non-canonical near-duplicate segments from this run "
                              "(see scripts/find_duplicates.py)")
    args = parser.parse_args()
    if args.include_duplicates:
        print("--include-duplicates: near-duplicate segments will NOT be excluded from this run.")

    summary = classifier.run_training_pass(
        progress_callback=print_progress, exclude_duplicates=not args.include_duplicates,
    )

    if summary["error"]:
        print(summary["error"])
        return

    trained = [r for r in summary["results"] if r["status"] == "trained"]
    if not trained:
        print(
            f"\nNo theme has {classifier.MIN_POSITIVES_ATTEMPT}+ coded segments yet. "
            "Keep coding, then re-run this script."
        )
    else:
        print(f"\nDone. model_version={summary['model_version']}")


if __name__ == "__main__":
    main()
