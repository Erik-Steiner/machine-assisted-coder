"""One-off: builds data/demo_dataset.json, a small real sample of interview
transcripts bundled with the repo so a fresh clone has something to browse
immediately, with no API key or download required.

Samples N records (default 10, fixed seed for reproducibility) from an
already-downloaded queries/*.json dataset and writes them, unmodified, to
data/demo_dataset.json -- viewer_server.py loads that file at startup as an
extra "demo" dataset, alongside "primary".

This is real transcript data (not fabricated), sourced from the same
ceointerviews.ai API every researcher's own downloads use. Confirm the
source API's terms allow redistributing a small sample before committing
this file to a public repo.

Usage:
    python scripts/make_demo_dataset.py                  # samples the "Google LLC" dataset
    python scripts/make_demo_dataset.py q_d90fd9aa45      # samples a specific dataset id
    python scripts/make_demo_dataset.py --n 15 "OpenAI"   # different size / label match
"""
import argparse
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import query_api
from paths import QUERIES_DIR

DEMO_FILE = HERE / "data" / "demo_dataset.json"
DEFAULT_SEED = 42


def find_entry(registry, selector):
    for e in registry:
        if e["id"] == selector:
            return e
    matches = [e for e in registry if selector.lower() in e["label"].lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f'"{selector}" matched multiple datasets -- be more specific:')
        for e in matches:
            print(f"  {e['id']}: {e['label']}")
        return None
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selector", nargs="?", default="Google LLC",
                         help="dataset id or a substring of its label (default: Google LLC)")
    parser.add_argument("--n", type=int, default=10, help="sample size (default: 10)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="random seed (default: 42)")
    args = parser.parse_args()

    registry = query_api.load_registry(QUERIES_DIR)
    entry = find_entry(registry, args.selector)
    if entry is None:
        print(f'No dataset matching "{args.selector}" found in {QUERIES_DIR / "_index.json"}.')
        sys.exit(1)

    json_path = QUERIES_DIR / entry["json_file"]
    with open(json_path, encoding="utf-8") as f:
        records = json.load(f)
    query_api.validate_dataset_records(records, json_path.name)

    if len(records) <= args.n:
        sample = records
    else:
        rng = random.Random(args.seed)
        sample = rng.sample(records, args.n)

    with open(DEMO_FILE, "w", encoding="utf-8") as f:
        json.dump(sample, f, indent=2, ensure_ascii=False)

    print(f'Wrote {len(sample)} interviews from "{entry["label"]}" to {DEMO_FILE.name}')
    print("This is real transcript data -- confirm the source API's terms allow "
          "redistributing it before committing this file to a public repo.")


if __name__ == "__main__":
    main()
