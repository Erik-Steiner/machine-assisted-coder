"""One-time migration: renames the pre-schema_version-2 record-level field
names (company/executive/entity_id/title/channel_name/feed_item_id/
enhanced_transcript_json) to their canonical, source-agnostic replacements
(group_name/person_name/person_id/person_title/source_name/item_id/turns)
in every dataset under queries/, plus demo_dataset.json and
executive_interviews.json if present -- see IMPORTING_DATA.md for the full
schema and coding_store.py's schema_version 2 migration for the matching
coding.db column renames.

Backs up queries/ first, unconditionally -- these are real, irreplaceable,
API-sourced datasets, and this script rewrites them in place. Idempotent:
re-running it on already-migrated files is a no-op (detected by the presence
of "group_name"/"item_id" keys).

Run once, by hand, before starting the server against renamed code:
    python scripts/migrate_field_names.py
"""
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import query_api
from paths import QUERIES_DIR

DEMO_FILE = HERE / "demo_dataset.json"
LEGACY_PRIMARY_FILE = HERE / "executive_interviews.json"

FIELD_RENAMES = {
    "company": "group_name",
    "executive": "person_name",
    "entity_id": "person_id",
    "title": "person_title",
    "channel_name": "source_name",
    "feed_item_id": "item_id",
    "enhanced_transcript_json": "turns",
}


def migrate_record(rec):
    out = {FIELD_RENAMES.get(k, k): v for k, v in rec.items()}
    if out.get("item_id") is not None:
        out["item_id"] = str(out["item_id"])
    return out


def already_migrated(records):
    return bool(records) and ("group_name" in records[0] or "item_id" in records[0])


def migrate_dataset_file(json_path):
    """A queries/*.json file: rewrite it and regenerate its paired .csv via
    query_api.write_export (reuse, don't reimplement CSV writing)."""
    with open(json_path, encoding="utf-8") as f:
        records = json.load(f)
    if already_migrated(records):
        return None, len(records)
    migrated = [migrate_record(r) for r in records]
    query_api.validate_dataset_records(migrated, json_path.name, require_item_id=False)
    query_api.write_export(json_path.parent, json_path.stem, migrated)
    return len(migrated), len(records)


def migrate_json_only(path):
    """demo_dataset.json / executive_interviews.json: no paired .csv to
    regenerate, so just rewrite the JSON in place."""
    with open(path, encoding="utf-8") as f:
        records = json.load(f)
    if already_migrated(records):
        return None, len(records)
    migrated = [migrate_record(r) for r in records]
    query_api.validate_dataset_records(migrated, path.name, require_item_id=False)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(migrated, f, indent=2, ensure_ascii=False)
    return len(migrated), len(records)


def main():
    if QUERIES_DIR.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_dir = QUERIES_DIR.parent / f"queries_backup_{stamp}"
        shutil.copytree(QUERIES_DIR, backup_dir)
        print(f"Backed up {QUERIES_DIR} to {backup_dir}\n")

    registry = query_api.load_registry(QUERIES_DIR)
    for entry in registry:
        json_path = QUERIES_DIR / entry["json_file"]
        if not json_path.exists():
            print(f"skip {entry['id']}: {json_path.name} not found on disk")
            continue
        after, before = migrate_dataset_file(json_path)
        if after is None:
            print(f"{entry['label']}: already migrated ({before} records)")
        else:
            print(f"{entry['label']}: migrated {before} -> {after} records")

    for extra in (DEMO_FILE, LEGACY_PRIMARY_FILE):
        if extra.exists():
            after, before = migrate_json_only(extra)
            if after is None:
                print(f"{extra.name}: already migrated ({before} records)")
            else:
                print(f"{extra.name}: migrated {before} -> {after} records")

    print("\nDone. Re-run scripts/build_segments.py to resegment against the renamed data,")
    print("then start the server -- coding_store.init_db() migrates coding.db's schema"
          " automatically on startup.")


if __name__ == "__main__":
    main()
