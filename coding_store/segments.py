"""Speaker-turn segments -- precomputed by scripts/build_segments.py from each
item's `turns` (see segmentation.py), one row per segment. Read by every
other coding_store submodule that needs segment text/metadata (review.py,
duplicates.py), but this module never reads codes/themes/etc. itself.
"""
from . import schema


def upsert_segments(segments):
    """Insert or replace a batch of segment dicts (see segmentation.py for shape).
    Idempotent -- safe to re-run scripts/build_segments.py."""
    now = schema._now()
    rows = [
        (
            s["segment_id"], str(s["item_id"]), s["dataset_id"], s.get("group_name"),
            s.get("person_name"), s["seg_index"], s.get("speaker_name"), s.get("speaker_id"),
            s.get("speaker_role"), s.get("depth"), s.get("timestamp"), s["text"],
            s.get("word_count"), now,
        )
        for s in segments
    ]
    with schema.get_conn() as conn:
        conn.executemany(
            """INSERT INTO segments
                 (segment_id, item_id, dataset_id, group_name, person_name, seg_index,
                  speaker_name, speaker_id, speaker_role, depth, timestamp, text,
                  word_count, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(segment_id) DO UPDATE SET
                 item_id=excluded.item_id, dataset_id=excluded.dataset_id,
                 group_name=excluded.group_name, person_name=excluded.person_name,
                 seg_index=excluded.seg_index, speaker_name=excluded.speaker_name,
                 speaker_id=excluded.speaker_id, speaker_role=excluded.speaker_role,
                 depth=excluded.depth, timestamp=excluded.timestamp,
                 text=excluded.text, word_count=excluded.word_count""",
            rows,
        )


def count_segments(dataset_id=None):
    with schema.get_conn() as conn:
        if dataset_id:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM segments WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM segments").fetchone()
    return row["n"]


def get_segments_for_item(item_id):
    """item_id is always compared/returned as a string -- some existing rows may still be
    stored with SQLite's INTEGER storage class (the column kept its original affinity
    across the schema_version 2 rename; SQLite's affinity-aware comparison still matches
    a string parameter against those rows correctly), but the dict this returns always
    normalizes item_id to str so callers (JSON responses, JS) see one consistent type."""
    with schema.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM segments WHERE item_id=? ORDER BY seg_index",
            (str(item_id),),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["item_id"] = str(d["item_id"])
        out.append(d)
    return out


def get_segments_by_ids(segment_ids):
    """Full segment rows (not just segment_id/text) for an arbitrary id list -- e.g. the exact
    set classifier.build_corpus() decided is in scope, so corpus_analytics.py can compute
    document/word/dataset-level stats over the same corpus the classifier actually trains on
    without re-deriving its filtering logic. Order isn't guaranteed to match segment_ids.
    Batched at schema.SQLITE_MAX_VARIABLES per query -- a single IN (...) with one placeholder
    per id blows past SQLite's own parameter-count limit once a real corpus's segment count
    (routinely tens of thousands) exceeds it."""
    if not segment_ids:
        return []
    out = []
    with schema.get_conn() as conn:
        for i in range(0, len(segment_ids), schema.SQLITE_MAX_VARIABLES):
            batch = segment_ids[i:i + schema.SQLITE_MAX_VARIABLES]
            placeholders = ",".join("?" * len(batch))
            rows = conn.execute(
                f"SELECT * FROM segments WHERE segment_id IN ({placeholders})", batch,
            ).fetchall()
            for r in rows:
                d = dict(r)
                d["item_id"] = str(d["item_id"])
                out.append(d)
    return out
