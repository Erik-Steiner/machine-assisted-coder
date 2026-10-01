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
                """INSERT INTO item_reviews (dataset_id, item_id, scheme_id, coder, marked_at)
                   VALUES (?,?,?,?,?)""",
                (dataset_id, item_id, scheme_id, coder, now),
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
