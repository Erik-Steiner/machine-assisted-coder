"""Codes: a coder's decision that a theme applies to a segment -- and its
counterpart, explicit rejections (the `negatives` table): a decision that a
theme does NOT apply. The two are mutually exclusive per segment/theme/coder. Provenance
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
    """Apply a code. Also soft-deletes this coder's active rejection of the same
    theme on the same segment, in the same transaction -- a code and a rejection
    are mutually exclusive per (segment, theme, coder); see add_negative()."""
    now = schema._now()
    with schema.get_conn() as conn:
        _soft_delete_negative(conn, segment_id, theme_id, coder, now)
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


# --- Explicit rejections ("negatives") -------------------------------------------------
#
# A rejection records that a coder looked at a segment and decided a theme does NOT
# apply -- different from simply never having coded it. Same provenance and
# soft-delete shape as codes (source/note/deleted_at). A code and a rejection for the
# same (segment, theme, coder) cancel each other: adding either soft-deletes the other.
# Removing a rejection only soft-deletes it; it never restores a code. Rejections
# don't change today's training labels (an uncoded segment is already a 0); they keep
# a rejected prediction out of future review queues, and are the explicit negatives
# the roadmap's `reviewed` negative mode (A6) will train on.

def _soft_delete_negative(conn, segment_id, theme_id, coder, now):
    conn.execute(
        """UPDATE negatives SET deleted_at=? WHERE segment_id=? AND theme_id=? AND coder=?
           AND deleted_at IS NULL""",
        (now, segment_id, theme_id, coder),
    )


def add_negative(segment_id, theme_id, coder=schema.DEFAULT_CODER, source="manual", note=None):
    """Record that theme_id does not apply to segment_id. Soft-deletes this coder's
    active code for the same theme on the same segment, in the same transaction."""
    now = schema._now()
    with schema.get_conn() as conn:
        conn.execute(
            """UPDATE codes SET deleted_at=? WHERE segment_id=? AND theme_id=? AND coder=?
               AND deleted_at IS NULL""",
            (now, segment_id, theme_id, coder),
        )
        existing = conn.execute(
            "SELECT negative_id, deleted_at FROM negatives WHERE segment_id=? AND theme_id=? AND coder=?",
            (segment_id, theme_id, coder),
        ).fetchone()
        if existing and existing["deleted_at"] is None:
            return  # already active, no-op
        if existing:
            # The table-level UNIQUE covers soft-deleted rows too, so reactivate in place.
            conn.execute(
                "UPDATE negatives SET deleted_at=NULL, source=?, note=?, created_at=? WHERE negative_id=?",
                (source, note, now, existing["negative_id"]),
            )
        else:
            conn.execute(
                """INSERT INTO negatives (segment_id, theme_id, coder, source, note, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (segment_id, theme_id, coder, source, note, now),
            )


def remove_negative(segment_id, theme_id, coder=schema.DEFAULT_CODER):
    with schema.get_conn() as conn:
        _soft_delete_negative(conn, segment_id, theme_id, coder, schema._now())


def get_negatives_for_segment_ids(segment_ids):
    """{segment_id: [theme_id, ...]} for active rejections on the given segments --
    the rejection counterpart of get_codes_for_segment_ids(), batched the same way."""
    if not segment_ids:
        return {}
    out = {}
    with schema.get_conn() as conn:
        for i in range(0, len(segment_ids), schema.SQLITE_MAX_VARIABLES):
            batch = segment_ids[i:i + schema.SQLITE_MAX_VARIABLES]
            placeholders = ",".join("?" * len(batch))
            rows = conn.execute(
                f"""SELECT segment_id, theme_id FROM negatives
                    WHERE deleted_at IS NULL AND segment_id IN ({placeholders})""",
                batch,
            ).fetchall()
            for r in rows:
                out.setdefault(r["segment_id"], []).append(r["theme_id"])
    return out
