"""One-off precompute: reads queries/*.json for the target datasets, segments
each interview via segmentation.segment_interview(), and upserts the results
into coding.db. Idempotent -- safe to re-run (e.g. after a new download adds
more interviews to one of these datasets).

Usage:
    python scripts/build_segments.py                    # every dataset in queries/_index.json
    python scripts/build_segments.py q_abc123 q_def456   # only these dataset ids
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import coding_store
import query_api
from paths import QUERIES_DIR
from segmentation import segment_interview


def main():
    registry = {e["id"]: e for e in query_api.load_registry(QUERIES_DIR)}
    dataset_ids = sys.argv[1:] or list(registry.keys())
    if not dataset_ids:
        print(f"No datasets found in {QUERIES_DIR / '_index.json'} -- run download_script.py "
              f"or Search & Export first.")
        return

    coding_store.init_db()

    for dataset_id in dataset_ids:
        entry = registry.get(dataset_id)
        if entry is None:
            print(f"skip {dataset_id}: not found in queries/_index.json")
            continue
        json_path = QUERIES_DIR / entry["json_file"]
        if not json_path.exists():
            print(f"skip {dataset_id}: {json_path.name} not found on disk")
            continue
        try:
            with open(json_path, encoding="utf-8") as f:
                records = json.load(f)
            query_api.validate_dataset_records(records, json_path.name, require_item_id=True)
        except (json.JSONDecodeError, query_api.DatasetValidationError) as exc:
            print(f"skip {dataset_id}: {exc}")
            continue

        all_segments = []
        for record in records:
            segs = segment_interview(record)
            for seg in segs:
                seg["dataset_id"] = dataset_id
            all_segments.extend(segs)

        coding_store.upsert_segments(all_segments)
        print(f"{entry['label']}: {len(records)} interviews -> {len(all_segments)} segments")

    print(f"Total segments in coding.db: {coding_store.count_segments()}")


if __name__ == "__main__":
    main()
