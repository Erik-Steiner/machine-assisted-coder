"""Codes: a coder's decision that a theme applies to a segment. Provenance
(coder/source/note) lives on the row itself rather than a separate audit
table -- see review.js's source="recoded" tagging. No activity_log calls
here; per-code provenance is already covered by the columns themselves.
"""
from . import schema


def get_codes_for_segment_ids(segment_ids):
    """Returns {segment_id: [theme_id, ...]} for active codes on the given segments.
    Batched at schema.SQLITE_MAX_VARIABLES per query -- see
    segments.get_segments_by_ids()'s docstring; review.get_review_candidates() can
    call this with every coded segment across a whole theme, which routinely exceeds
    SQLite's parameter-count limit in an actively-coded project."""
    if not segment_ids:
        return {}
    out = {}
    with schema.get_conn() as conn:
        for i in range(0, len(segment_ids), schema.SQLITE_MAX_VARIABLES):
            batch = segment_ids[i:i + schema.SQLITE_MAX_VARIABLES]
            placeholders = ",".join("?" * len(batch))
            rows = conn.execute(
                f"""SELECT segment_id, theme_id FROM codes
                    WHERE deleted_at IS NULL AND segment_id IN ({placeholders})""",
                batch,
            ).fetchall()
            for r in rows:
                out.setdefault(r["segment_id"], []).append(r["theme_id"])
    return out


def add_code(segment_id, theme_id, coder=schema.DEFAULT_CODER, source="manual", note=None):
    now = schema._now()
    with schema.get_conn() as conn:
        existing = conn.execute(
            """SELECT code_id, deleted_at FROM codes
               WHERE segment_id=? AND theme_id=? AND coder=?
               ORDER BY code_id DESC LIMIT 1""",
            (segment_id, theme_id, coder),
        ).fetchone()
        if existing and existing["deleted_at"] is None:
            return  # already active, no-op
        if existing:
            conn.execute(
                "UPDATE codes SET deleted_at=NULL, source=?, note=?, created_at=? WHERE code_id=?",
                (source, note, now, existing["code_id"]),
            )
        else:
            conn.execute(
                """INSERT INTO codes (segment_id, theme_id, coder, source, note, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (segment_id, theme_id, coder, source, note, now),
            )


def remove_code(segment_id, theme_id, coder=schema.DEFAULT_CODER):
    with schema.get_conn() as conn:
        conn.execute(
            """UPDATE codes SET deleted_at=? WHERE segment_id=? AND theme_id=? AND coder=?
               AND deleted_at IS NULL""",
            (schema._now(), segment_id, theme_id, coder),
        )


def add_negative(segment_id, theme_id, coder=schema.DEFAULT_CODER):
    with schema.get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO negatives (segment_id, theme_id, coder, created_at)
               VALUES (?,?,?,?)""",
            (segment_id, theme_id, coder, schema._now()),
        )
