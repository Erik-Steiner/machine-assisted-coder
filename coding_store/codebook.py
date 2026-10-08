"""Codebook completeness (docs/CODEBOOK_PLAN.md K2): how ready each theme's codebook
entry is to hand to a model -- an external LLM/embedding pipeline via the export (K7),
or the in-app embedding engine. A theme with a one-line description, no criteria and
no researcher-chosen examples gives any model little to go on.

Advisory only. Nothing here blocks coding, training or exporting; the Train tab's
Codebook sub-page, its banner, and the export dialog just flag what's missing.

theme_completeness() is the one rule (pure), used by the UI endpoint and the export
alike, so they can't disagree. The thresholds were confirmed by the researcher on
2026-10-07. Depends on schema, labels and themes.
"""
from . import labels, reviewed, schema, themes as themes_store

DEFINITION_MIN_WORDS = 15   # a sentence or two that actually says what the theme is
EXEMPLARS_MIN = 3           # researcher-flagged typical cases...
EXEMPLAR_GROUPS_MIN = 2     # ...from at least two groups, so they aren't one speaker's idiom
NEAR_MISSES_MIN = 2         # flagged "looks like it, isn't" contrasts

CHECK_LABELS = {
    "definition": "Definition",
    "criteria": "Inclusion and exclusion criteria",
    "exemplars": "Exemplars",
    "near_misses": "Near misses",
    "gold": "Positive in a read-in-full interview",
}


def theme_completeness(theme, counts):
    """{check: {"ok": bool, "label": str, "detail": str}} plus "score" (passed checks)
    and "total". `theme` is a themes row; `counts` has exemplars, exemplar_groups,
    near_misses and read_in_full_items_with_positive."""
    words = len((theme.get("description") or "").split())
    has_inc = bool((theme.get("inclusion_criteria") or "").strip())
    has_exc = bool((theme.get("exclusion_criteria") or "").strip())
    checks = {
        "definition": (words >= DEFINITION_MIN_WORDS,
                       f"{words}/{DEFINITION_MIN_WORDS} words"),
        "criteria": (has_inc and has_exc,
                     "both set" if has_inc and has_exc
                     else "missing " + " and ".join(x for x, ok in (("inclusion", has_inc), ("exclusion", has_exc)) if not ok)),
        "exemplars": (counts["exemplars"] >= EXEMPLARS_MIN and counts["exemplar_groups"] >= EXEMPLAR_GROUPS_MIN,
                      f"{counts['exemplars']}/{EXEMPLARS_MIN} flagged, from "
                      f"{counts['exemplar_groups']}/{EXEMPLAR_GROUPS_MIN} groups"),
        "near_misses": (counts["near_misses"] >= NEAR_MISSES_MIN,
                        f"{counts['near_misses']}/{NEAR_MISSES_MIN} flagged"),
        "gold": (counts["read_in_full_items_with_positive"] >= 1,
                 f"{counts['read_in_full_items_with_positive']} read-in-full interview(s) with a positive"),
    }
    out = {key: {"ok": ok, "label": CHECK_LABELS[key], "detail": detail} for key, (ok, detail) in checks.items()}
    out["score"] = sum(1 for key in checks if out[key]["ok"])
    out["total"] = len(checks)
    return out


def read_in_full_items(scheme_id):
    """Items whose mark counts as read in full -- the one rule lives in
    coding_store/reviewed.py (confirmed or interactive marks, not bulk ones)."""
    return reviewed.read_in_full_items(scheme_id)


def theme_counts(theme_id, read_in_full=None):
    """The counts theme_completeness() needs, for one theme. read_in_full: the scheme's
    read_in_full_items(), passed in when computing a whole scheme."""
    theme = themes_store.get_theme(theme_id)
    if read_in_full is None:
        read_in_full = read_in_full_items(theme["scheme_id"])
    with schema.get_conn() as conn:
        ex = conn.execute(
            """SELECT COUNT(*) AS n, COUNT(DISTINCT s.group_name) AS groups
               FROM codes c JOIN segments s ON s.segment_id = c.segment_id
               WHERE c.theme_id=? AND c.deleted_at IS NULL AND c.is_exemplar=1""",
            (theme_id,),
        ).fetchone()
        near = conn.execute(
            "SELECT COUNT(*) AS n FROM negatives WHERE theme_id=? AND deleted_at IS NULL AND is_near_miss=1",
            (theme_id,),
        ).fetchone()
        positives = list(labels.effective_positive_segment_ids(theme_id))
        items_with_positive = set()
        for i in range(0, len(positives), schema.SQLITE_MAX_VARIABLES):
            batch = positives[i:i + schema.SQLITE_MAX_VARIABLES]
            placeholders = ",".join("?" * len(batch))
            for r in conn.execute(
                f"SELECT DISTINCT dataset_id, item_id FROM segments WHERE segment_id IN ({placeholders})", batch,
            ):
                items_with_positive.add((r["dataset_id"], str(r["item_id"])))
    return {
        "positives": len(positives),
        "exemplars": ex["n"],
        "exemplar_groups": ex["groups"],
        "near_misses": near["n"],
        "read_in_full_items_with_positive": len(items_with_positive & read_in_full),
    }


def overview(scheme_id):
    """Every active theme in the scheme with its codebook fields, counts and
    completeness, in codebook order -- the Codebook sub-page's data."""
    read_in_full = read_in_full_items(scheme_id)
    out = []
    for t in themes_store.list_themes(scheme_id=scheme_id):
        counts = theme_counts(t["theme_id"], read_in_full=read_in_full)
        out.append({**t, "counts": counts, "completeness": theme_completeness(t, counts)})
    return out
