"""The researcher's codebook: themes, plus theme-level lifecycle (create,
edit, archive/restore, merge). Depends on activity_log (every mutation here
is logged) but not on codes.py beyond raw SQL against the codes table for
merge_themes()'s re-pointing.
"""
import re
import unicodedata

from . import activity_log, schema


def _slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return text or "theme"


def list_themes(include_archived=False):
    with schema.get_conn() as conn:
        if include_archived:
            rows = conn.execute("SELECT * FROM themes ORDER BY name").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM themes WHERE status='active' ORDER BY name"
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


def create_theme(name, description="", example_words="", color="#6b7fd7", actor=schema.DEFAULT_CODER):
    name = name.strip()
    if not name:
        raise ValueError("theme name is required")
    base_id = _slugify(name)
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
                                    status, created_at, updated_at)
               VALUES (?,?,?,?,?, 'active', ?, ?)""",
            (theme_id, name, description, example_words, color, now, now),
        )
    activity_log.log_activity(actor, "theme_create", {"theme_id": theme_id, "name": name, "description": description})
    return get_theme(theme_id)


def get_theme(theme_id):
    with schema.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM themes WHERE theme_id=?", (theme_id,)
        ).fetchone()
    return dict(row) if row else None


def update_theme(theme_id, fields, actor=schema.DEFAULT_CODER):
    allowed = {"name", "description", "example_words", "color"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_theme(theme_id)
    updates["updated_at"] = schema._now()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with schema.get_conn() as conn:
        conn.execute(
            f"UPDATE themes SET {set_clause} WHERE theme_id=?",
            (*updates.values(), theme_id),
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
    constraint would otherwise reject the re-point). negatives are merged the
    same way. source is then archived with merged_into set to target, rather
    than deleted, so the merge is auditable and reversible by hand if needed.
    """
    if source_theme_id == target_theme_id:
        raise ValueError("cannot merge a theme into itself")
    if get_theme(source_theme_id) is None or get_theme(target_theme_id) is None:
        raise ValueError("both themes must exist")

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

        source_negs = conn.execute(
            "SELECT * FROM negatives WHERE theme_id=?", (source_theme_id,)
        ).fetchall()
        for row in source_negs:
            conn.execute(
                """INSERT OR IGNORE INTO negatives (segment_id, theme_id, coder, created_at)
                   VALUES (?,?,?,?)""",
                (row["segment_id"], target_theme_id, row["coder"], now),
            )
            conn.execute("DELETE FROM negatives WHERE negative_id=?", (row["negative_id"],))

        conn.execute(
            "UPDATE themes SET status='archived', merged_into=?, updated_at=? WHERE theme_id=?",
            (target_theme_id, now, source_theme_id),
        )
    activity_log.log_activity(actor, "theme_merge", {
        "source_theme_id": source_theme_id, "target_theme_id": target_theme_id,
    })
    return get_theme(target_theme_id)
