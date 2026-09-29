"""Coding schemes: independent, concurrent codebooks. A scheme is the
top-level container themes.py's theme_id namespace is scoped by (see
themes.create_theme()) -- two schemes can each have a theme named the same
thing with zero collision. Same soft-lifecycle idiom as themes.py (archive/
restore only, no hard delete): a scheme may hold irreplaceable researcher
codes, so it's never deleted, only hidden from the active-scheme picker.

Deliberately no merge_schemes() -- merging two independent codebooks is a
fuzzy, out-of-scope operation that would undercut the reason schemes exist
(independence). Depends on activity_log (every mutation here is logged) but
not on themes.py/codes.py.
"""
import re
import unicodedata

from . import activity_log, schema


def _slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return text or "scheme"


def list_schemes(include_archived=False):
    with schema.get_conn() as conn:
        if include_archived:
            rows = conn.execute("SELECT * FROM schemes ORDER BY name").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM schemes WHERE status='active' ORDER BY name"
            ).fetchall()
        schemes = [dict(r) for r in rows]
        theme_counts = conn.execute(
            "SELECT scheme_id, COUNT(*) AS n FROM themes WHERE status='active' GROUP BY scheme_id"
        ).fetchall()
        code_counts = conn.execute(
            """SELECT t.scheme_id AS scheme_id, COUNT(*) AS n
               FROM codes c JOIN themes t ON t.theme_id = c.theme_id
               WHERE c.deleted_at IS NULL GROUP BY t.scheme_id"""
        ).fetchall()
    theme_count_by_scheme = {r["scheme_id"]: r["n"] for r in theme_counts}
    code_count_by_scheme = {r["scheme_id"]: r["n"] for r in code_counts}
    for s in schemes:
        s["theme_count"] = theme_count_by_scheme.get(s["scheme_id"], 0)
        s["code_count"] = code_count_by_scheme.get(s["scheme_id"], 0)
    return schemes


def create_scheme(name, description="", actor=schema.DEFAULT_CODER):
    name = name.strip()
    if not name:
        raise ValueError("scheme name is required")
    base_id = _slugify(name)
    now = schema._now()
    with schema.get_conn() as conn:
        scheme_id = base_id
        suffix = 1
        while conn.execute(
            "SELECT 1 FROM schemes WHERE scheme_id=?", (scheme_id,)
        ).fetchone():
            suffix += 1
            scheme_id = f"{base_id}_{suffix}"
        conn.execute(
            """INSERT INTO schemes (scheme_id, name, description, status, created_at, updated_at)
               VALUES (?,?,?, 'active', ?, ?)""",
            (scheme_id, name, description, now, now),
        )
    activity_log.log_activity(actor, "scheme_create", {"scheme_id": scheme_id, "name": name})
    return get_scheme(scheme_id)


def get_scheme(scheme_id):
    with schema.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM schemes WHERE scheme_id=?", (scheme_id,)
        ).fetchone()
    return dict(row) if row else None


def update_scheme(scheme_id, fields, actor=schema.DEFAULT_CODER):
    allowed = {"name", "description"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_scheme(scheme_id)
    updates["updated_at"] = schema._now()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with schema.get_conn() as conn:
        conn.execute(
            f"UPDATE schemes SET {set_clause} WHERE scheme_id=?",
            (*updates.values(), scheme_id),
        )
    activity_log.log_activity(actor, "scheme_update", {"scheme_id": scheme_id,
                               "fields": {k: v for k, v in updates.items() if k != "updated_at"}})
    return get_scheme(scheme_id)


def set_scheme_status(scheme_id, status, actor=schema.DEFAULT_CODER):
    with schema.get_conn() as conn:
        conn.execute(
            "UPDATE schemes SET status=?, updated_at=? WHERE scheme_id=?",
            (status, schema._now(), scheme_id),
        )
    activity_log.log_activity(actor, "scheme_archive" if status == "archived" else "scheme_restore",
                               {"scheme_id": scheme_id, "status": status})
    return get_scheme(scheme_id)
