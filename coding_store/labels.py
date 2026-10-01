"""Effective labels: what counts as a positive or an explicit negative for a
theme once the codebook's hierarchy is taken into account. The single place
that answers "is this segment theme T?" for training (classifier.build_labels),
the Model tab's counts (model_runs.get_value_prop / already_coded flags), and
coding progress (review.get_progress).

The rules (roadmap A2):
  - Roll-up: a code on a sub-code counts as a positive for every ancestor theme.
    Coding a passage "Pricing" (a child of "Market pressure") says it is market
    pressure, too. Without this, a parent's classifier learned the opposite of
    the codebook: every segment coded only with a sub-code was a negative.
  - Rejections flow down, never up: rejecting "Market pressure" implies "not
    Pricing", but rejecting "Pricing" says nothing about "Market pressure".
  - A positive wins over a rejection: if a segment is effectively positive for
    T (coded with T or a descendant) and also has a rejection that applies to T,
    it counts as positive. Same direction as merge_themes()' conflict rule.
  - Archived sub-codes still count. Archiving hides a theme from new coding;
    it doesn't make its existing codes untrue. (A merged theme's codes were
    re-pointed to the target, so they're counted there.)
  - Reviewed negatives (roadmap A6, reviewed_negative_segment_ids()): segments
    judged "fully coded" / "none of these" after a theme was created count against
    it, alongside its explicit rejections.

Nothing here writes anything: stored codes and rejections stay exactly as the
researcher entered them, and roll-up is computed on read. Depends only on
schema (raw SQL over themes/codes/negatives/item_reviews/
segment_none_confirmations), so model_runs.py and review.py can use it without
an import cycle.
"""
from . import schema

# Recursive CTEs over themes.parent_theme_id. Each binds one parameter, the theme_id,
# and yields that theme plus its descendants (SUBTREE) or plus its ancestors (LINEAGE).
SUBTREE_CTE = """subtree(theme_id) AS (
    SELECT ?
    UNION
    SELECT t.theme_id FROM themes t JOIN subtree s ON t.parent_theme_id = s.theme_id
)"""
LINEAGE_CTE = """lineage(theme_id) AS (
    SELECT ?
    UNION
    SELECT t.parent_theme_id FROM themes t JOIN lineage l ON t.theme_id = l.theme_id
    WHERE t.parent_theme_id IS NOT NULL
)"""

def effective_label_sql(theme_id, segment_col="p.segment_id"):
    """SQL fragments + params for "this segment is effectively coded / rejected for
    theme_id", for queries scoped to one theme (model_runs' prediction reads).
    The theme's subtree and lineage are resolved once here and inlined as IN (...)
    lists -- a per-row recursive subquery would re-walk the hierarchy for every
    one of tens of thousands of prediction rows. Returns
    (coded_sql, coded_params, rejected_sql, rejected_params); rejected_sql already
    excludes effectively-coded segments (a positive wins)."""
    subtree = sorted(subtree_theme_ids(theme_id))
    lineage = sorted(lineage_theme_ids(theme_id))
    sub_ph = ",".join("?" * len(subtree))
    lin_ph = ",".join("?" * len(lineage))
    coded_sql = f"""EXISTS (
        SELECT 1 FROM codes c WHERE c.segment_id = {segment_col} AND c.deleted_at IS NULL
          AND c.theme_id IN ({sub_ph}))"""
    rejected_sql = f"""(EXISTS (
        SELECT 1 FROM negatives n WHERE n.segment_id = {segment_col} AND n.deleted_at IS NULL
          AND n.theme_id IN ({lin_ph})) AND NOT {coded_sql})"""
    return coded_sql, list(subtree), rejected_sql, list(lineage) + list(subtree)


def subtree_theme_ids(theme_id):
    """theme_id plus every descendant theme (any status)."""
    with schema.get_conn() as conn:
        rows = conn.execute(f"WITH RECURSIVE {SUBTREE_CTE} SELECT theme_id FROM subtree", (theme_id,)).fetchall()
    return {r["theme_id"] for r in rows}


def lineage_theme_ids(theme_id):
    """theme_id plus every ancestor theme."""
    with schema.get_conn() as conn:
        rows = conn.execute(f"WITH RECURSIVE {LINEAGE_CTE} SELECT theme_id FROM lineage", (theme_id,)).fetchall()
    return {r["theme_id"] for r in rows}


def effective_positive_segment_ids(theme_id):
    """Segments with an active code (any coder) on theme_id or any descendant."""
    with schema.get_conn() as conn:
        rows = conn.execute(
            f"""WITH RECURSIVE {SUBTREE_CTE}
                SELECT DISTINCT segment_id FROM codes
                WHERE deleted_at IS NULL AND theme_id IN (SELECT theme_id FROM subtree)""",
            (theme_id,),
        ).fetchall()
    return {r["segment_id"] for r in rows}


def effective_rejected_segment_ids(theme_id):
    """Segments with an active rejection (any coder) on theme_id or any ancestor,
    minus any segment that is effectively positive for theme_id."""
    with schema.get_conn() as conn:
        rows = conn.execute(
            f"""WITH RECURSIVE {LINEAGE_CTE}
                SELECT DISTINCT segment_id FROM negatives
                WHERE deleted_at IS NULL AND theme_id IN (SELECT theme_id FROM lineage)""",
            (theme_id,),
        ).fetchall()
    return {r["segment_id"] for r in rows} - effective_positive_segment_ids(theme_id)


def reviewed_negative_segment_ids(theme_id):
    """Every segment that counts as a negative for theme_id in training's `reviewed`
    negative mode (roadmap A6): its explicit rejections (effective_rejected_segment_ids:
    own or an ancestor's), plus every segment the researcher judged for this theme's
    scheme *after the theme was created* -- inside an item marked "fully coded", or
    confirmed "none of these themes" on its own (coding_store/reviewed.py) -- minus
    anything effectively positive (a positive wins). A mark made before the theme
    existed says nothing about it: that theme was never considered.

    Not filtered to the training corpus (interviewer turns, short fragments,
    duplicates): classifier.training_labels() intersects with that itself."""
    with schema.get_conn() as conn:
        theme = conn.execute(
            "SELECT scheme_id, created_at FROM themes WHERE theme_id=?", (theme_id,)
        ).fetchone()
        if theme is None:
            return set()
        rows = conn.execute(
            """SELECT s.segment_id FROM item_reviews r
               JOIN segments s ON s.dataset_id = r.dataset_id AND CAST(s.item_id AS TEXT) = r.item_id
               WHERE r.scheme_id = ? AND r.unmarked_at IS NULL AND r.marked_at >= ?
               UNION
               SELECT segment_id FROM segment_none_confirmations
               WHERE scheme_id = ? AND unconfirmed_at IS NULL AND confirmed_at >= ?""",
            (theme["scheme_id"], theme["created_at"], theme["scheme_id"], theme["created_at"]),
        ).fetchall()
    judged = {r["segment_id"] for r in rows}
    return (judged | effective_rejected_segment_ids(theme_id)) - effective_positive_segment_ids(theme_id)


def roll_up_counts(code_rows, parent_by_theme):
    """Pure helper: {theme_id: number of distinct segments effectively positive for
    it}, given code_rows as (segment_id, theme_id) pairs and parent_by_theme as
    {theme_id: parent_theme_id or None}. Each code is credited to its theme and
    every ancestor. A cycle in parent_by_theme (which themes.update_theme() refuses
    to create) is cut rather than looped on."""
    segments_by_theme = {}
    for segment_id, theme_id in code_rows:
        current, seen = theme_id, set()
        while current is not None and current not in seen:
            seen.add(current)
            segments_by_theme.setdefault(current, set()).add(segment_id)
            current = parent_by_theme.get(current)
    return {tid: len(segs) for tid, segs in segments_by_theme.items()}
