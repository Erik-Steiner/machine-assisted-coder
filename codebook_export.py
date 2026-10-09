"""Exports one scheme's codebook in a form an external model can consume without
hand-editing (docs/CODEBOOK_PLAN.md K7): codebook_topic_themes.json (definitions,
criteria, boundary notes, researcher-flagged exemplars and near misses, completeness)
and codebook_labels.csv (every known positive/negative label per segment and theme).

Why a separate export: the existing ones don't carry what a model needs.
/api/export/codebook is a raw table dump (backup / supplementary material), and
topic_model_export.py's topic_seed_pack fills `exemplars` with the classifier's own top
predictions -- coded and uncoded mixed together, so an off-theme passage the model
liked could be handed to the next model as a "typical case". Building that by hand
took the researcher a day (CODEBOOK_PLAN "Why this exists"). Both existing exports
stay as they are.

Rules this module keeps, and why:
  - exemplars / near_misses are only researcher-flagged passages
    (coding_store/examples.py). No fallback to predictions when a theme has none: the
    list stays empty and `completeness` says so.
  - Twins (near-identical passages from different records) collapse into one entry:
    the earliest-flagged passage of a twin group is kept, the others are listed in its
    twin_segment_ids, so a model never sees the same example twice.
  - Archived themes are included with their status (the consumer decides); merged
    themes are not, since their codes already point at the target.
  - codebook_version hashes the canonical JSON of `topics` only, so two exports of an
    unchanged codebook get the same version and an external run can record exactly
    which codebook it used. generated_at is deliberately outside the hash, and so is
    everything derived rather than authored (DERIVED_TOPIC_KEYS, DERIVED_EXAMPLE_KEYS).
    current_codebook_version() computes the same value without counting labels or
    loading records, so every training run can record it (CODEBOOK_PLAN U2).
  - Labels come from coding_store/labels.py, the single source of truth for what is
    positive or negative. Negatives use its read-in-full rule
    (reviewed_negative_segment_ids(read_in_full_only=True)): explicit rejections, "none
    of these" confirmations, and uncoded segments only in interviews marked read in
    full (confirmed or interactive). Uncoded segments in unconfirmed bulk-marked
    interviews are neutral here, because many of those interviews were never read end
    to end (CODEBOOK_PLAN K5). This module only *attributes* each label to a source;
    it never decides membership itself.
  - turn_indices (K6) link each exported segment back to its source turns, via
    segmentation.segment_turn_indices(record). Records are read through an injected
    load_records(dataset_id) callable (the server passes
    viewer_server._load_dataset_records_with_turns), and only for segments that appear
    in the export. If the function or the record isn't available, the field is empty.

Read-only apart from one activity_log row, like topic_model_export.py and
appendix_export.py.
"""
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import segmentation
from coding_store import (
    activity_log, codebook, examples, labels, reviewed, schema, schemes, segments as segments_store, themes,
)

FORMAT = "codebook_topic_themes"
FORMAT_VERSION = 1
JSON_NAME = "codebook_topic_themes.json"
CSV_NAME = "codebook_labels.csv"

LABEL_FIELDS = [
    "segment_id", "dataset_id", "item_id", "group_name", "person_name", "speaker_role",
    "turn_indices", "theme_id", "label", "label_source", "item_read_in_full_status",
    "inferred_by_coverage", "is_exemplar", "is_near_miss", "twin_group",
]
EXAMPLE_FIELDS = ["segment_id", "dataset_id", "item_id", "group_name", "person_name",
                  "speaker_name", "speaker_role", "text", "note"]
COMPLETENESS_CHECKS = ["definition", "criteria", "exemplars", "near_misses", "gold"]


def label_policy():
    return {
        "positive": "an active code on the theme or any of its sub-codes (roll-up, A2)",
        "negative": "an explicit rejection, a 'none of these' confirmation, or an uncoded non-interviewer "
                    "segment in a read-in-full interview, judged after the theme was created",
        "neutral": "everything else; absent from codebook_labels.csv",
        "read_in_full_counts": list(reviewed.READ_IN_FULL_STATUSES),
        "inferred_by_coverage_min_coded": reviewed.READ_IN_FULL_MIN_CODED,
    }


# Per-topic keys derived from coding activity rather than written by the researcher. They're
# left out of codebook_version, so the version changes when the codebook itself changes
# (definitions, criteria, notes, chosen examples), not every time a passage is coded.
DERIVED_TOPIC_KEYS = ("counts", "completeness")
# Per-example keys derived from the source records: turn_indices is empty whenever the
# records weren't loaded (no load_records, or an unreadable dataset). Left out of the hash so
# the version doesn't depend on whether they were available.
DERIVED_EXAMPLE_KEYS = ("turn_indices",)
EXAMPLE_LISTS = ("exemplars", "near_misses")


def codebook_version(topics):
    """"sha256:" + the hash of the canonical JSON (sorted keys, compact separators) of the
    researcher-authored part of topics -- everything except DERIVED_TOPIC_KEYS, and
    DERIVED_EXAMPLE_KEYS inside each example."""
    authored = []
    for t in topics:
        entry = {k: v for k, v in t.items() if k not in DERIVED_TOPIC_KEYS}
        for kind in EXAMPLE_LISTS:
            if kind in entry:
                entry[kind] = [{k: v for k, v in e.items() if k not in DERIVED_EXAMPLE_KEYS}
                               for e in entry[kind]]
        authored.append(entry)
    canonical = json.dumps(authored, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def collapse_twins(rows):
    """rows: an examples.annotate_twins() result (oldest flag first). Returns
    (kept, twin_group): kept is the first row of each twin group, with
    twin_segment_ids listing the rest; twin_group maps every segment_id in a group of
    two or more to the kept segment_id, which serves as the group's id. Twins are
    grouped transitively (a~b, b~c puts a, b, c together)."""
    by_id = {r["segment_id"]: r for r in rows}
    seen, kept, twin_group = set(), [], {}
    for r in rows:
        if r["segment_id"] in seen:
            continue
        group, stack = [], [r["segment_id"]]
        while stack:
            sid = stack.pop()
            if sid in seen or sid not in by_id:
                continue
            seen.add(sid)
            group.append(sid)
            stack.extend(by_id[sid].get("twin_of", []))
        others = sorted(sid for sid in group if sid != r["segment_id"])
        kept.append({**r, "twin_segment_ids": others})
        if others:
            for sid in group:
                twin_group[sid] = r["segment_id"]
    return kept, twin_group


def _flagged(theme_id, kind):
    """examples.list_flagged(), one row per segment (two coders flagging the same
    passage is still one example), with twin links."""
    rows, seen = [], set()
    for r in examples.list_flagged(theme_id, kind):
        if r["segment_id"] not in seen:
            seen.add(r["segment_id"])
            rows.append(r)
    return examples.annotate_twins(rows)


def _direct_positive_ids(theme_id):
    """Segments with an active code on theme_id itself (not via a sub-code) -- only to
    tell `code` from `code_rollup`; the positive set itself is labels.py's."""
    with schema.get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT segment_id FROM codes WHERE theme_id=? AND deleted_at IS NULL", (theme_id,),
        ).fetchall()
    return {r["segment_id"] for r in rows}


def _none_confirmed_ids(theme):
    """Active "none of these" confirmations that count for this theme (made after it
    was created) -- only to attribute a negative's source; membership is labels.py's."""
    with schema.get_conn() as conn:
        rows = conn.execute(
            """SELECT segment_id FROM segment_none_confirmations
               WHERE scheme_id=? AND unconfirmed_at IS NULL AND confirmed_at >= ?""",
            (theme["scheme_id"], theme["created_at"]),
        ).fetchall()
    return {r["segment_id"] for r in rows}


def _mark_statuses(scheme_id):
    """{(dataset_id, item_id): reviewed.mark_status()} for every item with an active
    mark in the scheme (the latest mark wins, as in reviewed.get_item_review())."""
    with schema.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM item_reviews WHERE scheme_id=? AND unmarked_at IS NULL ORDER BY marked_at",
            (scheme_id,),
        ).fetchall()
    return {(r["dataset_id"], str(r["item_id"])): reviewed.mark_status(dict(r)) for r in rows}


class TurnIndexLookup:
    """segment_id -> 0-based indices into its record's `turns` (K6). Loads each
    dataset at most once and segments only the records that are needed."""

    def __init__(self, load_records):
        self.load_records = load_records
        self.fn = getattr(segmentation, "segment_turn_indices", None)
        self._records = {}  # dataset_id -> {item_id: record} or None
        self._by_segment = {}
        self._done_items = set()

    def available(self):
        return self.fn is not None and self.load_records is not None

    def get(self, segment_id, dataset_id, item_id):
        if not self.available() or not dataset_id or item_id is None:
            return []
        key = (dataset_id, str(item_id))
        if key not in self._done_items:
            self._done_items.add(key)
            if dataset_id not in self._records:
                records = self.load_records(dataset_id)
                self._records[dataset_id] = (
                    {str(r.get("item_id")): r for r in records} if records else None
                )
            record = (self._records[dataset_id] or {}).get(str(item_id))
            if record is not None:
                segs = segmentation.segment_interview(record)
                for seg, indices in zip(segs, self.fn(record)):
                    self._by_segment[seg["segment_id"]] = list(indices)
        return self._by_segment.get(segment_id, [])


def _example_entry(row, turns):
    entry = {f: row.get(f) for f in EXAMPLE_FIELDS}
    entry["item_id"] = str(entry["item_id"]) if entry["item_id"] is not None else None
    entry["turn_indices"] = turns.get(row["segment_id"], row.get("dataset_id"), row.get("item_id"))
    entry["twin_segment_ids"] = row.get("twin_segment_ids", [])
    return entry


def _export_themes(scheme_id):
    """Every theme in the scheme except merged ones, in codebook order."""
    return [t for t in themes.list_themes(include_archived=True, scheme_id=scheme_id) if not t.get("merged_into")]


def _authored_topic(t, exemplars, near_misses, turns):
    """One topic's researcher-authored entry: what codebook_version hashes. The export
    adds counts and completeness; current_codebook_version() uses it as is, so the two
    can't drift apart. exemplars / near_misses are collapse_twins() results."""
    return {
        "topic_id": t["theme_id"],
        "name": t["name"],
        "parent_topic_id": t.get("parent_theme_id"),
        "status": t["status"],
        "definition": t.get("description") or "",
        "inclusion_criteria": t.get("inclusion_criteria") or "",
        "exclusion_criteria": t.get("exclusion_criteria") or "",
        "boundary_notes": t.get("boundary_notes") or "",
        "exemplars": [_example_entry(r, turns) for r in exemplars],
        "near_misses": [_example_entry(r, turns) for r in near_misses],
    }


def current_codebook_version(scheme_id):
    """The codebook_version an export of this scheme would get right now, without
    counting labels or loading records: cheap enough for every training run to record
    which codebook it was trained under (CODEBOOK_PLAN U2). Raises ValueError for an
    unknown scheme."""
    if schemes.get_scheme(scheme_id) is None:
        raise ValueError(f"unknown scheme '{scheme_id}'")
    turns = TurnIndexLookup(None)
    topics = []
    for t in _export_themes(scheme_id):
        ex_kept, _ = collapse_twins(_flagged(t["theme_id"], "exemplar"))
        nm_kept, _ = collapse_twins(_flagged(t["theme_id"], "near_miss"))
        topics.append(_authored_topic(t, ex_kept, nm_kept, turns))
    return codebook_version(topics)


def codebook_versions(scheme_id=None):
    """{scheme_id: current_codebook_version()} for one scheme, or for every active scheme
    when scheme_id is None (a training pass over every scheme)."""
    scheme_ids = [scheme_id] if scheme_id else [s["scheme_id"] for s in schemes.list_schemes()]
    return {sid: current_codebook_version(sid) for sid in scheme_ids}


def build_codebook_export(scheme_id, load_records=None):
    """Builds (document, label_rows, completeness_summary) without writing anything.
    Raises ValueError for an unknown scheme."""
    scheme = schemes.get_scheme(scheme_id)
    if scheme is None:
        raise ValueError(f"unknown scheme '{scheme_id}'")
    theme_list = _export_themes(scheme_id)
    read_in_full = reviewed.read_in_full_items(scheme_id)
    mark_statuses = _mark_statuses(scheme_id)
    coded_counts = reviewed.coded_segment_counts(scheme_id)
    turns = TurnIndexLookup(load_records)

    per_theme, all_label_ids = [], set()
    for t in theme_list:
        tid = t["theme_id"]
        positives = labels.effective_positive_segment_ids(tid)
        negatives = labels.reviewed_negative_segment_ids(tid, read_in_full_only=True)
        rejected = labels.effective_rejected_segment_ids(tid)
        ex_rows, nm_rows = _flagged(tid, "exemplar"), _flagged(tid, "near_miss")
        ex_kept, ex_twins = collapse_twins(ex_rows)
        nm_kept, nm_twins = collapse_twins(nm_rows)
        per_theme.append({
            "theme": t, "positives": positives, "negatives": negatives, "rejected": rejected,
            "direct": _direct_positive_ids(tid), "none_confirmed": _none_confirmed_ids(t),
            "exemplars": ex_kept, "near_misses": nm_kept,
            # A segment is either coded (exemplar) or rejected (near miss) for a theme, never
            # both, so one map serves both kinds.
            "twin_group": {**nm_twins, **ex_twins},
            "exemplar_ids": {r["segment_id"] for r in ex_rows},
            "near_miss_ids": {r["segment_id"] for r in nm_rows},
        })
        all_label_ids |= positives | negatives

    seg_meta = {s["segment_id"]: s for s in segments_store.get_segments_by_ids(sorted(all_label_ids))}
    # Interviewer turns are never offered for coding (the Code tab shows no chips on them),
    # so a read-in-full interview's *uncoded* interviewer turns weren't judged: they're
    # neutral, not negatives. Training drops them anyway (classifier.build_corpus()); an
    # external model reading the labels file would otherwise learn "questions aren't the
    # theme". An explicit rejection or "none of these" on one is a real judgment and stays.
    interviewer = {sid for sid, s in seg_meta.items() if s.get("speaker_role") == "interviewer"}
    for p in per_theme:
        p["negatives"] = p["negatives"] - (interviewer - p["rejected"] - p["none_confirmed"])

    topics, label_rows, summary = [], [], []
    for p in per_theme:
        t = p["theme"]
        counts = codebook.theme_counts(t["theme_id"], read_in_full=read_in_full)
        counts["explicit_rejections"] = len(p["rejected"])
        counts["reviewed_negatives"] = len(p["negatives"])
        completeness = codebook.theme_completeness(t, counts)
        topic = _authored_topic(t, p["exemplars"], p["near_misses"], turns)
        topic["completeness"] = {k: completeness[k]["ok"] for k in COMPLETENESS_CHECKS}
        topic["counts"] = counts
        topics.append(topic)
        summary.append({
            "theme_id": t["theme_id"], "name": t["name"], "status": t["status"],
            "score": completeness["score"], "total": completeness["total"],
            "missing": [completeness[k]["label"] for k in COMPLETENESS_CHECKS if not completeness[k]["ok"]],
        })

        labeled = [(sid, "pos") for sid in p["positives"]] + [(sid, "neg") for sid in p["negatives"]]
        for sid, label in sorted(labeled):
            if label == "pos":
                source = "code" if sid in p["direct"] else "code_rollup"
            elif sid in p["rejected"]:
                source = "explicit_rejection"  # wins when several sources apply
            elif sid in p["none_confirmed"]:
                source = "none_confirmed"
            else:
                source = "read_in_full_uncoded"
            seg = seg_meta.get(sid, {})
            dataset_id, item_id = seg.get("dataset_id"), seg.get("item_id")
            item_key = (dataset_id, str(item_id))
            label_rows.append({
                "segment_id": sid, "dataset_id": dataset_id or "", "item_id": item_id if item_id is not None else "",
                "group_name": seg.get("group_name") or "", "person_name": seg.get("person_name") or "",
                "speaker_role": seg.get("speaker_role") or "",
                "turn_indices": ";".join(str(i) for i in turns.get(sid, dataset_id, item_id)),
                "theme_id": t["theme_id"], "label": label, "label_source": source,
                "item_read_in_full_status": mark_statuses.get(item_key, "none") if seg else "",
                "inferred_by_coverage": _bool(coded_counts.get(item_key, 0) >= reviewed.READ_IN_FULL_MIN_CODED),
                "is_exemplar": _bool(sid in p["exemplar_ids"]),
                "is_near_miss": _bool(sid in p["near_miss_ids"]),
                "twin_group": p["twin_group"].get(sid, ""),
            })

    document = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": schema.CURRENT_SCHEMA_VERSION,
        "scheme": {"scheme_id": scheme["scheme_id"], "name": scheme["name"],
                   "description": scheme.get("description") or ""},
        "codebook_version": codebook_version(topics),
        "label_policy": label_policy(),
        "turn_indices_available": turns.available(),
        "topics": topics,
    }
    return document, label_rows, summary


def _bool(value):
    return "true" if value else "false"


def export_codebook_themes(output_dir, scheme_id, load_records=None, actor=schema.DEFAULT_CODER):
    """Writes codebook_topic_themes.json and codebook_labels.csv for one scheme into
    output_dir and logs `codebook_themes_export`. load_records(dataset_id) -> raw
    records with `turns` (or None), for turn_indices. Returns {paths, codebook_version,
    completeness: [{theme_id, name, status, score, total, missing}], label_rows,
    turn_indices_available}. Raises ValueError for an unknown scheme."""
    document, label_rows, summary = build_codebook_export(scheme_id, load_records=load_records)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / JSON_NAME
    csv_path = output_dir / CSV_NAME
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2, ensure_ascii=False)
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LABEL_FIELDS)
        writer.writeheader()
        writer.writerows(label_rows)
    paths = [json_path, csv_path]
    activity_log.log_activity(actor, "codebook_themes_export", {
        "scheme_id": scheme_id, "codebook_version": document["codebook_version"],
        "topics": len(document["topics"]), "label_rows": len(label_rows),
        "files": [p.name for p in paths],
    })
    return {"paths": paths, "codebook_version": document["codebook_version"], "completeness": summary,
            "label_rows": len(label_rows), "turn_indices_available": document["turn_indices_available"]}
