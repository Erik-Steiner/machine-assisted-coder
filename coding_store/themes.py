"""The researcher's codebook: themes, plus theme-level lifecycle (create,
edit, archive/restore, merge). Depends on activity_log (every mutation here
is logged) but not on codes.py beyond raw SQL against the codes table for
merge_themes()'s re-pointing.

Every theme belongs to exactly one scheme (see schemes.py) -- theme_id is
namespaced as f"{scheme_id}__{slugified name}" so two schemes can each have a
theme named the same thing with zero collision, while theme_id itself stays a
single globally-unique TEXT primary key that codes/negatives/predictions/
model_runs/model_top_terms can keep referencing exactly as before (no schema
change needed in any of those tables -- scoping is transitive through
theme_id). Within a scheme, a theme may optionally have a parent_theme_id,
forming a hierarchy from broad themes down to specific emic sub-codes;
depth is a cached indentation level recomputed whenever parent_theme_id
changes, not a source of truth.
"""
import re
import unicodedata

from . import activity_log, schema


def _slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return text or "theme"


def list_themes(include_archived=False, scheme_id=None):
    where = ["status='active'"] if not include_archived else []
    params = []
    if scheme_id is not None:
        where.append("scheme_id=?")
        params.append(scheme_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with schema.get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM themes {clause} ORDER BY depth, sort_order, name", params
        ).fetchall()
        themes = [dict(r) for r in rows]
        counts = conn.execute(
            """SELECT theme_id, COUNT(*) AS n FROM codes
               WHERE deleted_at IS NULL GROUP BY theme_id"""
        ).fetchall()
    count_by_theme = {r["theme_id"]: r["n"] for r in counts}
    for t in themes:
        t["code_count"] = count_by_theme.get(t["theme_id"], 0)
    return themes


def create_theme(name, description="", example_words="", color="#6b7fd7",
                  scheme_id=None, parent_theme_id=None, actor=schema.DEFAULT_CODER,
                  inclusion_criteria=None, exclusion_criteria=None, boundary_notes=None):
    name = name.strip()
    if not name:
        raise ValueError("theme name is required")
    if not scheme_id:
        raise ValueError("scheme_id is required")

    parent = None
    if parent_theme_id:
        parent = get_theme(parent_theme_id)
        if parent is None:
            raise ValueError("parent theme does not exist")
        if parent["scheme_id"] != scheme_id:
            raise ValueError("parent theme must belong to the same scheme")
    depth = (parent["depth"] + 1) if parent else 0

    base_id = f"{scheme_id}__{_slugify(name)}"
    now = schema._now()
    with schema.get_conn() as conn:
        theme_id = base_id
        suffix = 1
        while conn.execute(
            "SELECT 1 FROM themes WHERE theme_id=?", (theme_id,)
        ).fetchone():
            suffix += 1
            theme_id = f"{base_id}_{suffix}"
        conn.execute(
            """INSERT INTO themes (theme_id, name, description, example_words, color,
                                    status, scheme_id, parent_theme_id, depth, sort_order,
                                    created_at, updated_at,
                                    inclusion_criteria, exclusion_criteria, boundary_notes)
               VALUES (?,?,?,?,?, 'active', ?,?,?,0, ?, ?, ?,?,?)""",
            (theme_id, name, description, example_words, color,
             scheme_id, parent_theme_id, depth, now, now,
             inclusion_criteria, exclusion_criteria, boundary_notes),
        )
    activity_log.log_activity(actor, "theme_create", {
        "theme_id": theme_id, "name": name, "description": description,
        "scheme_id": scheme_id, "parent_theme_id": parent_theme_id,
    })
    return get_theme(theme_id)


def get_theme(theme_id):
    with schema.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM themes WHERE theme_id=?", (theme_id,)
        ).fetchone()
    return dict(row) if row else None


def _descendant_ids(conn, theme_id, scheme_id):
    """theme_id's descendants within its own scheme, walked breadth-first from
    the flat per-scheme theme list -- codebooks here are small (tens/low
    hundreds of themes), so this is cheap without needing a recursive CTE."""
    rows = conn.execute(
        "SELECT theme_id, parent_theme_id FROM themes WHERE scheme_id=?", (scheme_id,)
    ).fetchall()
    children_by_parent = {}
    for r in rows:
        children_by_parent.setdefault(r["parent_theme_id"], []).append(r["theme_id"])
    out = []
    frontier = [theme_id]
    while frontier:
        current = frontier.pop()
        for child_id in children_by_parent.get(current, []):
            out.append(child_id)
            frontier.append(child_id)
    return out


def update_theme(theme_id, fields, actor=schema.DEFAULT_CODER):
    allowed = {"name", "description", "example_words", "color", "parent_theme_id", "sort_order",
               "inclusion_criteria", "exclusion_criteria", "boundary_notes"}  # last three: CODEBOOK_PLAN K2
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_theme(theme_id)

    theme = get_theme(theme_id)
    if theme is None:
        raise ValueError("theme does not exist")

    reparenting = "parent_theme_id" in updates
    new_depth = None
    if reparenting:
        new_parent_id = updates["parent_theme_id"]
        if new_parent_id:
            if new_parent_id == theme_id:
                raise ValueError("a theme cannot be its own parent")
            new_parent = get_theme(new_parent_id)
            if new_parent is None:
                raise ValueError("parent theme does not exist")
            if new_parent["scheme_id"] != theme["scheme_id"]:
                raise ValueError("parent theme must belong to the same scheme")
            with schema.get_conn() as conn:
                if new_parent_id in _descendant_ids(conn, theme_id, theme["scheme_id"]):
                    raise ValueError("cannot move a theme under its own descendant")
            new_depth = new_parent["depth"] + 1
        else:
            new_depth = 0
        updates["depth"] = new_depth

    updates["updated_at"] = schema._now()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with schema.get_conn() as conn:
        conn.execute(
            f"UPDATE themes SET {set_clause} WHERE theme_id=?",
            (*updates.values(), theme_id),
        )
        if reparenting:
            # depth is a cache, not source of truth -- cascade the shift down
            # to every descendant so their indentation stays consistent.
            shift = new_depth - theme["depth"]
            if shift:
                for descendant_id in _descendant_ids(conn, theme_id, theme["scheme_id"]):
                    conn.execute(
                        "UPDATE themes SET depth = depth + ? WHERE theme_id=?",
                        (shift, descendant_id),
                    )
    activity_log.log_activity(actor, "theme_update", {"theme_id": theme_id,
                                          "fields": {k: v for k, v in updates.items() if k != "updated_at"}})
    return get_theme(theme_id)


def set_theme_status(theme_id, status, actor=schema.DEFAULT_CODER):
    with schema.get_conn() as conn:
        conn.execute(
            "UPDATE themes SET status=?, updated_at=? WHERE theme_id=?",
            (status, schema._now(), theme_id),
        )
    activity_log.log_activity(actor, "theme_archive" if status == "archived" else "theme_restore",
                 {"theme_id": theme_id, "status": status})
    return get_theme(theme_id)


def merge_themes(source_theme_id, target_theme_id, actor=schema.DEFAULT_CODER):
    """Merge source into target: every segment coded with source becomes coded
    with target instead -- the code row is re-pointed in place (same code_id,
    coder, source, note, created_at), so its association with the text block
    and its provenance both survive. If a segment already has an active target
    code from the same coder, the now-redundant source code is soft-deleted
    rather than duplicated (the unique-active-code-per-segment/theme/coder
    constraint would otherwise reject the re-point). Rejections (negatives) are
    merged the same way, never hard-deleted, and the code/rejection exclusivity
    rule (codes.add_negative()) holds afterwards: where a coder ends up with both
    a code and a rejection of target on one segment, the code wins. source is then archived with merged_into set to target, rather
    than deleted, so the merge is auditable and reversible by hand if needed.
    """
    if source_theme_id == target_theme_id:
        raise ValueError("cannot merge a theme into itself")
    source_theme = get_theme(source_theme_id)
    target_theme = get_theme(target_theme_id)
    if source_theme is None or target_theme is None:
        raise ValueError("both themes must exist")
    if source_theme["scheme_id"] != target_theme["scheme_id"]:
        raise ValueError("cannot merge themes across different schemes")

    now = schema._now()
    with schema.get_conn() as conn:
        source_codes = conn.execute(
            "SELECT * FROM codes WHERE theme_id=? AND deleted_at IS NULL",
            (source_theme_id,),
        ).fetchall()
        for row in source_codes:
            existing = conn.execute(
                """SELECT 1 FROM codes WHERE segment_id=? AND theme_id=? AND coder=?
                   AND deleted_at IS NULL""",
                (row["segment_id"], target_theme_id, row["coder"]),
            ).fetchone()
            if existing:
                conn.execute("UPDATE codes SET deleted_at=? WHERE code_id=?", (now, row["code_id"]))
            else:
                conn.execute("UPDATE codes SET theme_id=? WHERE code_id=?", (target_theme_id, row["code_id"]))
            # A code now on target cancels the same coder's rejection of target on this
            # segment (codes.add_code()'s rule) -- the merge says source *is* target.
            conn.execute(
                """UPDATE negatives SET deleted_at=? WHERE segment_id=? AND theme_id=? AND coder=?
                   AND deleted_at IS NULL""",
                (now, row["segment_id"], target_theme_id, row["coder"]),
            )

        # Rejections move the same way as codes: re-pointed in place (keeping
        # their provenance), or soft-deleted if redundant -- never hard-deleted.
        # Two differences from codes: negatives' UNIQUE(segment, theme, coder)
        # also covers soft-deleted rows, so a soft-deleted target row is
        # reactivated rather than re-pointed onto; and a rejection is dropped
        # (soft-deleted) when the same coder has the target theme *coded* on that
        # segment, since a code and a rejection can't coexist (codes.add_negative()).
        source_negs = conn.execute(
            "SELECT * FROM negatives WHERE theme_id=? AND deleted_at IS NULL", (source_theme_id,)
        ).fetchall()
        for row in source_negs:
            target_coded = conn.execute(
                """SELECT 1 FROM codes WHERE segment_id=? AND theme_id=? AND coder=?
                   AND deleted_at IS NULL""",
                (row["segment_id"], target_theme_id, row["coder"]),
            ).fetchone()
            target_neg = conn.execute(
                "SELECT negative_id, deleted_at FROM negatives WHERE segment_id=? AND theme_id=? AND coder=?",
                (row["segment_id"], target_theme_id, row["coder"]),
            ).fetchone()
            if target_coded or (target_neg and target_neg["deleted_at"] is None):
                conn.execute("UPDATE negatives SET deleted_at=? WHERE negative_id=?", (now, row["negative_id"]))
            elif target_neg:
                conn.execute(
                    "UPDATE negatives SET deleted_at=NULL, source=?, note=?, created_at=? WHERE negative_id=?",
                    (row["source"], row["note"], row["created_at"], target_neg["negative_id"]),
                )
                conn.execute("UPDATE negatives SET deleted_at=? WHERE negative_id=?", (now, row["negative_id"]))
            else:
                conn.execute("UPDATE negatives SET theme_id=? WHERE negative_id=?",
                             (target_theme_id, row["negative_id"]))

        conn.execute(
            "UPDATE themes SET status='archived', merged_into=?, updated_at=? WHERE theme_id=?",
            (target_theme_id, now, source_theme_id),
        )
    activity_log.log_activity(actor, "theme_merge", {
        "source_theme_id": source_theme_id, "target_theme_id": target_theme_id,
    })
    return get_theme(target_theme_id)
