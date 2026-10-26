"""Thin client for the ceointerviews.ai API, plus the shared queries/ export
format (JSON + CSV per query, registered in queries/_index.json).

Used by viewer_server.py's Search & Export tab and by download_script.py's
bulk per-company downloads — both write into the same folder, in the same
shape, so every dataset (hand-searched or bulk-downloaded) browses the same
way with no special-casing.
"""
import csv
import io
import json
import os
import re
import threading
import time
import unicodedata
from datetime import date
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

# One writer at a time per process for anything that rewrites an existing dataset file
# (append_to_dataset, update_record_metadata): both are read-modify-write.
DATASET_WRITE_LOCK = threading.Lock()


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


def _atomic_write_text(path, text):
    """Write to a sibling temp file, then os.replace() it over path, so a crash or a
    sync client (the researcher's project folder may live in Dropbox) never sees a
    half-written dataset file -- once records get rewritten in place (appends,
    metadata edits), a truncated JSON file would lose the only copy of them.
    os.replace() can briefly fail on Windows while another process holds the
    target open (a sync client or virus scanner), hence the short retry."""
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    for attempt in range(5):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2 * (attempt + 1))


def write_export(queries_dir, base_name, rows):
    """Write one query result as JSON + CSV into queries_dir, in the shape every
    dataset (hand-searched or bulk-downloaded) shares. Returns (json_path, csv_path).
    Safe to call repeatedly with the same base_name to checkpoint progress mid-fetch.
    """
    queries_dir = Path(queries_dir)
    queries_dir.mkdir(exist_ok=True)
    json_path = queries_dir / f"{base_name}.json"
    csv_path = queries_dir / f"{base_name}.csv"

    _atomic_write_text(json_path, json.dumps(rows, indent=2, ensure_ascii=False))

    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    _atomic_write_text(csv_path, buf.getvalue())

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
    with DATASET_WRITE_LOCK:
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


# --- Editing one record's metadata in place (Browse's "Edit details") ----------------
#
# A dataset's queries/*.json file is the only full copy of its records' metadata, so
# corrections are written there (JSON and CSV together, via write_export), not into an
# override layer every reader of the file would have to remember to apply. Provenance
# lives in activity_log instead: the caller logs each field's old and new value.

EDITABLE_FIELDS = [
    "item_title", "publish_date", "source_name", "source_url",
    "person_name", "person_title", "group_name", "duration_secs",
]
# Deliberately not editable: item_id (every segment_id -- and so every code -- is derived
# from it; for transcript imports it's a hash of the original group/person/title/date, and
# must keep that original value after those fields are edited), person_id (the source's
# own key), turns/transcript_text (changing text means re-segmenting), and the counts a
# source API reported (view_count/like_count/combined_classifier_score).


def _clean_metadata_value(field, value):
    """Normalizes one submitted value: strings stripped, "" -> None. publish_date must
    start with a valid YYYY-MM-DD (any time-of-day suffix is kept as given); duration_secs
    must be a non-negative number. Raises DatasetValidationError naming the field."""
    if value is None:
        return None
    if field == "duration_secs":
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise DatasetValidationError("duration_secs must be a number of seconds")
        if num < 0:
            raise DatasetValidationError("duration_secs can't be negative")
        return int(num) if num.is_integer() else num
    value = str(value).strip()
    if not value:
        return None
    if field == "publish_date":
        try:
            date.fromisoformat(value[:10])
        except ValueError:
            raise DatasetValidationError("publish_date must start with a date as YYYY-MM-DD")
    return value


def update_record_metadata(queries_dir, dataset_id, item_id, changes):
    """Applies `changes` ({field: new_value}, fields from EDITABLE_FIELDS only) to the one
    record with this item_id in a registered dataset, rewriting its JSON + CSV. Values
    equal to the current one are ignored, so a form can send every field it shows.

    Raises KeyError if dataset_id isn't registered (primary/demo aren't -- they're
    repo-level files) or no record has this item_id; DatasetValidationError for an
    unknown field, an invalid value, or an item_id that matches more than one record
    (ambiguous -- nothing is written).

    Returns {field: {"from": old, "to": new}} for the fields that actually changed
    (empty if none did, in which case nothing is written). Caller is responsible for
    syncing segments.group_name/person_name in coding.db and invalidating any in-memory
    cache of this dataset."""
    unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
    if unknown:
        raise DatasetValidationError(f"not an editable field: {', '.join(unknown)}")
    cleaned = {field: _clean_metadata_value(field, value) for field, value in changes.items()}

    queries_dir = Path(queries_dir)
    with DATASET_WRITE_LOCK:
        entry = next((e for e in load_registry(queries_dir) if e["id"] == dataset_id), None)
        if entry is None:
            raise KeyError(f"no registered dataset with id {dataset_id!r}")
        json_path = queries_dir / entry["json_file"]
        with open(json_path, encoding="utf-8") as f:
            records = json.load(f)
        matches = [r for r in records if r.get("item_id") is not None and str(r["item_id"]) == str(item_id)]
        if not matches:
            raise KeyError(f"no record with item_id {item_id!r} in dataset {dataset_id!r}")
        if len(matches) > 1:
            raise DatasetValidationError(
                f"item_id {item_id!r} matches {len(matches)} records in this dataset -- "
                "can't tell which one to edit"
            )
        record = matches[0]
        diff = {
            field: {"from": record.get(field), "to": new}
            for field, new in cleaned.items() if record.get(field) != new
        }
        if diff:
            for field, change in diff.items():
                record[field] = change["to"]
            write_export(queries_dir, Path(entry["json_file"]).stem, records)
    return diff


def dedupe_rows(rows):
    """Collapse rows to one per (person_id, item_id), keeping the last seen.
    Guards against the same interview being written twice by a checkpointed run."""
    seen = {}
    for r in rows:
        seen[(r.get("person_id"), r.get("item_id"))] = r
    return list(seen.values())
