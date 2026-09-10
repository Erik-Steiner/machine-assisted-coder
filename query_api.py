"""Thin client for the ceointerviews.ai API, plus the shared queries/ export
format (JSON + CSV per query, registered in queries/_index.json).

Used by viewer_server.py's Search & Export tab and by download_script.py's
bulk per-company downloads — both write into the same folder, in the same
shape, so every dataset (hand-searched or bulk-downloaded) browses the same
way with no special-casing.
"""
import csv
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("API_KEY")
BASE_URL = (os.getenv("BASE_URL") or "").rstrip("/")


class ApiCredentialsError(RuntimeError):
    pass


def _require_creds():
    if not API_KEY or not BASE_URL:
        raise ApiCredentialsError(
            "API_KEY and BASE_URL must be set (found in .env). "
            "Check that .env sits next to this script and defines both."
        )


def api_get(endpoint, **params):
    """GET one API page. Retries transient (5xx) errors."""
    _require_creds()
    url = f"{BASE_URL}/api/{endpoint}/?{urlencode(params)}"
    for attempt in range(4):
        try:
            resp = requests.get(url, headers={"X-API-Key": API_KEY}, timeout=90)
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp.json()
        except requests.exceptions.RequestException:
            if attempt == 3:
                raise
        time.sleep(2 ** attempt)  # 1s, 2s, 4s between retries
    raise RuntimeError(f"{endpoint} kept returning 5xx: {url}")


def search_companies(keyword, page_size=25):
    return api_get("get_companies", keyword=keyword, page_size=page_size).get("results", [])


def search_entities(keyword, page_size=25):
    return api_get("get_entities", keyword=keyword, page_size=page_size).get("results", [])


def fetch_feed(*, company_id=None, entity_id=None, after_dt=None, before_dt=None,
                page_size=500, on_page=None):
    """Keyset-paginate get_feed for one company_id or entity_id.

    `on_page(total_items_so_far, page_has_next)` is called after each page, if given,
    so a caller can report progress.
    """
    if not company_id and not entity_id:
        raise ValueError("fetch_feed needs company_id or entity_id")

    items = []
    last_seen_id = None
    while True:
        params = {"page_size": page_size}
        if company_id:
            params["company_id"] = company_id
        if entity_id:
            params["entity_id"] = entity_id
        if after_dt:
            params["filter_after_dt"] = after_dt
        if before_dt:
            params["filter_before_dt"] = before_dt
        if last_seen_id is not None:
            params["before_feed_item_id"] = last_seen_id

        data = api_get("get_feed", **params)
        page_items = data.get("results", [])
        items.extend(page_items)
        has_next = bool(data.get("page_has_next", False))
        if on_page:
            on_page(len(items), has_next)

        last_seen_id = data.get("last_seen_id")
        if not has_next or last_seen_id is None:
            break
    return items


def build_transcript_text(item):
    """Plain, speaker-labeled text for NLP use. Prefers enhanced_transcript
    (already clean speaker turns); falls back to parsing the raw SRT blocks
    for older items that lack it."""
    segments = item.get("enhanced_transcript") or []
    if segments:
        lines = [
            f"[{seg.get('speaker_name', 'unknown')}] {seg['content'].strip()}"
            for seg in segments
            if seg.get("content")
        ]
        return "\n".join(lines)

    raw_blocks = item.get("transcript") or []
    words = []
    for block in raw_blocks:
        parts = block.split("\n")
        caption = " ".join(parts[2:]) if len(parts) > 2 else ""
        if caption.strip():
            words.append(caption.strip())
    return " ".join(words)


def build_row(item, company_name):
    """Flatten one get_feed item into the row shape used for JSON/CSV export --
    this is effectively "the ceointerviews.ai importer": it maps that API's raw
    response shape into the app's canonical record schema, the same job
    scripts/import_interview_transcript.py and scripts/import_reddit.py do for
    their own sources. item_id is cast to str -- the canonical schema treats
    every source's item id as a string (see docs/IMPORTING_DATA.md), since ids from
    other sources (Reddit submission ids, etc.) aren't numeric."""
    extra = item.get("extra") or {}
    return {
        "group_name": company_name,
        "person_name": item.get("entity_name"),
        "person_title": item.get("entity_title"),
        "person_id": item.get("entity_id"),
        "item_id": str(item["feed_item_id"]) if item.get("feed_item_id") is not None else None,
        "item_title": item.get("item_title"),
        "source_url": item.get("source_url"),
        "publish_date": item.get("publish_date"),
        "duration_secs": extra.get("duration_secs"),
        "view_count": extra.get("view_count"),
        "like_count": extra.get("like_count"),
        "combined_classifier_score": extra.get("combined_classifier_score"),
        "source_name": extra.get("channel_name"),
        "transcript_text": build_transcript_text(item),
        "turns": item.get("enhanced_transcript") or [],
    }


def slugify(text, max_len=40):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return (text or "query")[:max_len]


# --- Shared queries/ export format -------------------------------------------------

EXPORT_FIELDS = [
    "group_name", "person_name", "person_title", "person_id", "item_id", "item_title",
    "source_url", "publish_date", "duration_secs", "view_count", "like_count",
    "combined_classifier_score", "source_name", "transcript_text",
    "turns",
]
CSV_FIELDS = [f for f in EXPORT_FIELDS if f != "turns"]


class DatasetValidationError(ValueError):
    """A dataset JSON file doesn't match the shape the app needs to load it safely."""


def validate_dataset_records(records, source_label, require_item_id=False):
    """Structural check for a dataset loaded from JSON (either a researcher's own
    file, or one of ours). docs/DEVELOPMENT.md's data contract for Browse is deliberately
    loose -- any flat array of objects works -- so this only rejects shapes that
    would otherwise fail confusingly deep inside build_dataset()/segment_interview()
    (a non-list top level, or non-object entries), plus one field-specific check:
    segment_interview() derives segment_id from item_id, so records missing it
    would all collide on the same id and silently overwrite each other's segments
    in coding.db. That check is opt-in (require_item_id) since Browse-only
    datasets don't need item_id at all -- only the Coding pipeline does.
    Raises DatasetValidationError naming the source file and the exact problem.
    """
    if not isinstance(records, list):
        raise DatasetValidationError(
            f"{source_label}: expected a JSON array of records, got {type(records).__name__}"
        )
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise DatasetValidationError(
                f"{source_label}: record {i} is a {type(rec).__name__}, expected an object"
            )
        if require_item_id and rec.get("item_id") is None:
            raise DatasetValidationError(
                f"{source_label}: record {i} has no item_id -- segmentation derives "
                "each segment's id from it, so every record missing one would collide on "
                "the same segment ids and overwrite each other in coding.db"
            )
    return records


def load_registry(queries_dir):
    index_file = Path(queries_dir) / "_index.json"
    if not index_file.exists():
        return []
    try:
        return json.loads(index_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_registry(queries_dir, entries):
    queries_dir = Path(queries_dir)
    queries_dir.mkdir(exist_ok=True)
    (queries_dir / "_index.json").write_text(json.dumps(entries, indent=2), encoding="utf-8")


def write_export(queries_dir, base_name, rows):
    """Write one query result as JSON + CSV into queries_dir, in the shape every
    dataset (hand-searched or bulk-downloaded) shares. Returns (json_path, csv_path).
    Safe to call repeatedly with the same base_name to checkpoint progress mid-fetch.
    """
    queries_dir = Path(queries_dir)
    queries_dir.mkdir(exist_ok=True)
    json_path = queries_dir / f"{base_name}.json"
    csv_path = queries_dir / f"{base_name}.csv"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    return json_path, csv_path


def register_export(queries_dir, *, dataset_id, label, kind, source_id, created_at,
                     count, json_path, csv_path):
    """Append one completed export to queries/_index.json so it shows up in the
    Browse tab's dataset dropdown."""
    entries = load_registry(queries_dir)
    entries.append({
        "id": dataset_id,
        "label": label,
        "kind": kind,
        "source_id": source_id,
        "created_at": created_at,
        "count": count,
        "json_file": Path(json_path).name,
        "csv_file": Path(csv_path).name,
    })
    save_registry(queries_dir, entries)
    return entries[-1]


_LABEL_COUNT_SUFFIX_RE = re.compile(r"\s+—\s+\d+\s+\S+\(s\)$")


def relabel_with_count(label, count, noun="interview(s)"):
    """Regenerates a registry label with an updated count, preserving
    whatever researcher-facing name came before the " -- N <noun>" suffix
    register_export() adds (or adding that suffix fresh, if the label didn't
    have one) -- used when appending to an existing dataset via
    append_to_dataset() so the count in Browse's dataset dropdown doesn't go
    stale. noun defaults to "interview(s)" (interview-transcript imports);
    pass e.g. "submission(s)" for a Reddit dataset so the wording matches
    what register_export() used when the dataset was first created. The
    strip regex matches any "<count> <word>(s)" suffix, not just
    "interview(s)", so relabeling works regardless of which noun a dataset's
    label already carries."""
    base = _LABEL_COUNT_SUFFIX_RE.sub("", label)
    return f"{base} — {count} {noun}"


def append_to_dataset(queries_dir, dataset_id, new_records, count_noun="interview(s)"):
    """Adds new_records to an existing queries/*.json export in place, so a
    researcher can import one more interview (or Reddit submission) into an
    existing bucket (e.g. every transcript from one research project)
    instead of every import creating its own one-item dataset -- the way
    company downloads already group many executives' interviews under one
    dataset_id. Updates the registry's count/label to match; see
    relabel_with_count() for count_noun.

    Raises KeyError if dataset_id isn't registered, or DatasetValidationError
    if any new record's item_id already exists in the dataset (segment_id is
    derived from item_id -- silently allowing this would let the new
    record's segments overwrite the existing item's in coding.db).

    Returns (combined_records, updated_registry_entry). Caller is
    responsible for re-segmenting only new_records (not combined_records --
    the existing ones are already in coding.db) and for invalidating any
    in-memory cache of this dataset."""
    queries_dir = Path(queries_dir)
    entries = load_registry(queries_dir)
    idx = next((i for i, e in enumerate(entries) if e["id"] == dataset_id), None)
    if idx is None:
        raise KeyError(f"no registered dataset with id {dataset_id!r}")
    entry = entries[idx]
    json_path = queries_dir / entry["json_file"]
    with open(json_path, encoding="utf-8") as f:
        existing = json.load(f)
    existing_ids = {r.get("item_id") for r in existing}
    collisions = sorted({r.get("item_id") for r in new_records if r.get("item_id") in existing_ids})
    if collisions:
        raise DatasetValidationError(
            f"item_id {', '.join(collisions)} already exists in this dataset -- adjust the item "
            "title or publish date so it hashes to a different id, or pick a different dataset"
        )
    combined = existing + new_records
    write_export(queries_dir, Path(entry["json_file"]).stem, combined)
    entries[idx] = {
        **entry, "count": len(combined),
        "label": relabel_with_count(entry["label"], len(combined), noun=count_noun),
    }
    save_registry(queries_dir, entries)
    return combined, entries[idx]


def dedupe_rows(rows):
    """Collapse rows to one per (person_id, item_id), keeping the last seen.
    Guards against the same interview being written twice by a checkpointed run."""
    seen = {}
    for r in rows:
        seen[(r.get("person_id"), r.get("item_id"))] = r
    return list(seen.values())
