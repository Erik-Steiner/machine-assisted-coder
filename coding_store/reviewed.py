"""Which material a researcher has actually judged, per coding scheme: whole items
marked "fully coded" (item_reviews) and single segments confirmed "none of these
themes" (segment_none_confirmations). See the schema comment on those tables.

Why it matters: in the `reviewed` negative mode (classifier.py), only passages the
researcher has judged count as negatives -- an unread segment in an interview no
one has opened is unlabeled, not evidence against a theme. coding_store/labels.py's
reviewed_negative_segment_ids() turns these rows into negatives, applying the rule
that a mark only counts against themes that already existed when it was made.

Nothing is ever hard-deleted: unmarking/unconfirming stamps unmarked_at/
unconfirmed_at, and marking again adds a fresh row. Item marks are also logged
to activity_log (they're deliberate, citable assertions about the corpus);
per-segment confirmations aren't -- their rows are their own record, the same
convention codes.py follows. Depends on schema and activity_log only.
"""
from . import activity_log, schema


def get_item_review(dataset_id, item_id, scheme_id):
    """The active "fully coded" mark for this item and scheme, or None."""
    with schema.get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM item_reviews WHERE dataset_id=? AND item_id=? AND scheme_id=?
               AND unmarked_at IS NULL ORDER BY marked_at DESC LIMIT 1""",
            (dataset_id, str(item_id), scheme_id),
        ).fetchone()
    return dict(row) if row else None


def set_item_review(dataset_id, item_id, scheme_id, reviewed, coder=schema.DEFAULT_CODER):
    """Mark (reviewed=True) or unmark an item as fully coded for a scheme. Marking
    an already-marked item is a no-op that keeps the original marked_at -- the
    original time is what decides which themes the mark covers."""
    item_id = str(item_id)
    now = schema._now()
    with schema.get_conn() as conn:
        active = conn.execute(
            """SELECT review_id FROM item_reviews WHERE dataset_id=? AND item_id=? AND scheme_id=?
               AND unmarked_at IS NULL""",
            (dataset_id, item_id, scheme_id),
        ).fetchall()
        if reviewed:
            if active:
                return get_item_review(dataset_id, item_id, scheme_id)
            conn.execute(
                """INSERT INTO item_reviews (dataset_id, item_id, scheme_id, coder, marked_at, recorded_at)
                   VALUES (?,?,?,?,?,?)""",
                (dataset_id, item_id, scheme_id, coder, now, now),
            )
        else:
            if not active:
                return None
            conn.execute(
                """UPDATE item_reviews SET unmarked_at=? WHERE dataset_id=? AND item_id=?
                   AND scheme_id=? AND unmarked_at IS NULL""",
                (now, dataset_id, item_id, scheme_id),
            )
    activity_log.log_activity(coder, "item_marked_fully_coded" if reviewed else "item_unmarked_fully_coded",
                              {"dataset_id": dataset_id, "item_id": item_id, "scheme_id": scheme_id})
    return get_item_review(dataset_id, item_id, scheme_id)


def backfill_candidates(scheme_id):
    """Items with at least one active code in scheme_id and no active "fully coded"
    mark for it -- the starting list for bulk_mark_items(), for a project coded
    before marks existed. Each: {dataset_id, item_id, coded_segments, last_coded_at}."""
    with schema.get_conn() as conn:
        rows = conn.execute(
            """SELECT s.dataset_id, CAST(s.item_id AS TEXT) AS item_id,
                      COUNT(DISTINCT c.segment_id) AS coded_segments, MAX(c.created_at) AS last_coded_at
               FROM codes c
               JOIN segments s ON s.segment_id = c.segment_id
               JOIN themes t ON t.theme_id = c.theme_id
               WHERE c.deleted_at IS NULL AND t.scheme_id = ?
                 AND NOT EXISTS (
                   SELECT 1 FROM item_reviews r WHERE r.scheme_id = t.scheme_id AND r.unmarked_at IS NULL
                     AND r.dataset_id = s.dataset_id AND r.item_id = CAST(s.item_id AS TEXT))
               GROUP BY s.dataset_id, CAST(s.item_id AS TEXT)
               ORDER BY last_coded_at""",
            (scheme_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def bulk_mark_items(items, scheme_id, as_of, coder=schema.DEFAULT_CODER):
    """Mark several items "fully coded" at once, dated as of an earlier time:
    as_of="last_code" dates each item's mark to its own latest active code in the
    scheme (so themes created after you last worked on it aren't covered), or an
    ISO timestamp (already normalized to UTC by the caller) dates them all to that
    moment. recorded_at is always now, so the record shows the marks were entered
    later. Items already marked, or (for "last_code") with no codes in the scheme,
    are skipped. One activity_log entry for the batch. Returns {marked, skipped}."""
    candidates = {(c["dataset_id"], c["item_id"]): c for c in backfill_candidates(scheme_id)}
    now = schema._now()
    marked, skipped = [], []
    with schema.get_conn() as conn:
        for dataset_id, item_id in items:
            item_id = str(item_id)
            already = conn.execute(
                """SELECT 1 FROM item_reviews WHERE dataset_id=? AND item_id=? AND scheme_id=?
                   AND unmarked_at IS NULL""",
                (dataset_id, item_id, scheme_id),
            ).fetchone()
            candidate = candidates.get((dataset_id, item_id))
            if already or (as_of == "last_code" and candidate is None):
                skipped.append({"dataset_id": dataset_id, "item_id": item_id})
                continue
            marked_at = candidate["last_coded_at"] if as_of == "last_code" else as_of
            conn.execute(
                """INSERT INTO item_reviews (dataset_id, item_id, scheme_id, coder, marked_at, recorded_at)
                   VALUES (?,?,?,?,?,?)""",
                (dataset_id, item_id, scheme_id, coder, marked_at, now),
            )
            marked.append({"dataset_id": dataset_id, "item_id": item_id, "marked_at": marked_at})
    activity_log.log_activity(coder, "items_bulk_marked_fully_coded", {
        "scheme_id": scheme_id, "as_of": as_of, "marked": len(marked), "skipped": len(skipped),
        "items": marked,
    })
    return {"marked": marked, "skipped": skipped}


def set_segment_none(segment_id, scheme_id, confirmed, coder=schema.DEFAULT_CODER):
    """Confirm (or withdraw) "none of this scheme's themes apply to this segment"."""
    now = schema._now()
    with schema.get_conn() as conn:
        active = conn.execute(
            """SELECT confirmation_id FROM segment_none_confirmations WHERE segment_id=? AND scheme_id=?
               AND unconfirmed_at IS NULL""",
            (segment_id, scheme_id),
        ).fetchone()
        if confirmed and not active:
            conn.execute(
                """INSERT INTO segment_none_confirmations (segment_id, scheme_id, coder, confirmed_at)
                   VALUES (?,?,?,?)""",
                (segment_id, scheme_id, coder, now),
            )
        elif not confirmed and active:
            conn.execute(
                """UPDATE segment_none_confirmations SET unconfirmed_at=? WHERE segment_id=? AND scheme_id=?
                   AND unconfirmed_at IS NULL""",
                (now, segment_id, scheme_id),
            )


def get_none_confirmed_segment_ids(segment_ids, scheme_id):
    """The subset of segment_ids with an active "none of these" confirmation for scheme_id."""
    if not segment_ids:
        return set()
    out = set()
    with schema.get_conn() as conn:
        for i in range(0, len(segment_ids), schema.SQLITE_MAX_VARIABLES - 1):
            batch = segment_ids[i:i + schema.SQLITE_MAX_VARIABLES - 1]
            placeholders = ",".join("?" * len(batch))
            rows = conn.execute(
                f"""SELECT segment_id FROM segment_none_confirmations
                    WHERE scheme_id=? AND unconfirmed_at IS NULL AND segment_id IN ({placeholders})""",
                (scheme_id, *batch),
            ).fetchall()
            out.update(r["segment_id"] for r in rows)
    return out
