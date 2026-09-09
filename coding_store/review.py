"""Review tab: the cross-interview recode queue, and coding-progress counts
(shared by the Coding tab's progress bars and Model's "no model yet"
fallback). Depends on codes.py (get_codes_for_segment_ids) and model_runs.py
(get_latest_model_runs, get_top_predictions) -- this is the one submodule
that reads across both.
"""
from . import codes, model_runs, schema


def get_review_candidates(theme_ids, predicted_limit=30):
    """Assemble a recode/review queue for one or more themes: every segment
    currently coded under any of theme_ids, plus (for themes with a trained
    model) each theme's top predicted_limit scored segments not already coded
    under that specific theme. Deduplicated by segment_id; each candidate
    carries a `reasons` list explaining why it's in the queue, e.g.
    [{"type": "coded", "theme_id": "marketization"},
     {"type": "predicted", "theme_id": "integrations", "score": 0.42}].
    Every candidate's `theme_ids` field lists ALL of its current active
    codes (not just ones in theme_ids), so the UI can show full context."""
    if not theme_ids:
        return []

    placeholders = ",".join("?" * len(theme_ids))
    with schema.get_conn() as conn:
        coded_rows = conn.execute(
            f"""SELECT DISTINCT s.segment_id, s.item_id, s.dataset_id, s.group_name,
                       s.person_name, s.speaker_name, s.speaker_role, s.depth,
                       s.word_count, s.timestamp, s.text
                FROM segments s
                JOIN codes c ON c.segment_id = s.segment_id
                WHERE c.theme_id IN ({placeholders}) AND c.deleted_at IS NULL""",
            theme_ids,
        ).fetchall()

    candidates = {}  # segment_id -> candidate dict

    def ensure_candidate(row):
        sid = row["segment_id"]
        if sid not in candidates:
            candidates[sid] = {
                "segment_id": sid, "item_id": str(row["item_id"]),
                "dataset_id": row["dataset_id"], "group_name": row["group_name"],
                "person_name": row["person_name"], "speaker_name": row["speaker_name"],
                "speaker_role": row["speaker_role"], "depth": row["depth"],
                "word_count": row["word_count"],
                "timestamp": row["timestamp"], "text": row["text"], "reasons": [],
            }
        return candidates[sid]

    coded_segment_ids = []
    for row in coded_rows:
        ensure_candidate(row)
        coded_segment_ids.append(row["segment_id"])

    if coded_segment_ids:
        seg_codes = codes.get_codes_for_segment_ids(coded_segment_ids)
        for sid, tids in seg_codes.items():
            for tid in tids:
                if tid in theme_ids:
                    candidates[sid]["reasons"].append({"type": "coded", "theme_id": tid})

    latest_runs = model_runs.get_latest_model_runs()
    for theme_id in theme_ids:
        run = latest_runs.get(theme_id)
        if not run:
            continue
        for p in model_runs.get_top_predictions(theme_id, run["model_version"], limit=predicted_limit):
            if p["already_coded"]:
                continue
            c = ensure_candidate(p)
            c["reasons"].append({"type": "predicted", "theme_id": theme_id, "score": p["score"]})

    all_ids = list(candidates.keys())
    codes_by_segment = codes.get_codes_for_segment_ids(all_ids)
    for sid, c in candidates.items():
        c["theme_ids"] = codes_by_segment.get(sid, [])

    return list(candidates.values())


def get_progress(dataset_id=None):
    with schema.get_conn() as conn:
        seg_filter = "WHERE dataset_id=?" if dataset_id else ""
        seg_params = (dataset_id,) if dataset_id else ()
        total_segments = conn.execute(
            f"SELECT COUNT(*) AS n FROM segments {seg_filter}", seg_params
        ).fetchone()["n"]

        coded_filter = "WHERE s.dataset_id=?" if dataset_id else ""
        coded_segments = conn.execute(
            f"""SELECT COUNT(DISTINCT c.segment_id) AS n
                FROM codes c JOIN segments s ON s.segment_id = c.segment_id
                {coded_filter} {"AND" if coded_filter else "WHERE"} c.deleted_at IS NULL""",
            seg_params,
        ).fetchone()["n"]

        theme_filter = "WHERE s.dataset_id=?" if dataset_id else ""
        by_theme_rows = conn.execute(
            f"""SELECT c.theme_id, t.name, COUNT(*) AS n
                FROM codes c
                JOIN segments s ON s.segment_id = c.segment_id
                LEFT JOIN themes t ON t.theme_id = c.theme_id
                {theme_filter} {"AND" if theme_filter else "WHERE"} c.deleted_at IS NULL
                GROUP BY c.theme_id""",
            seg_params,
        ).fetchall()

        group_filter = "WHERE dataset_id=?" if dataset_id else ""
        by_group_total = conn.execute(
            f"""SELECT group_name, COUNT(*) AS n FROM segments {group_filter}
                GROUP BY group_name""",
            seg_params,
        ).fetchall()
        group_coded_filter = "WHERE s.dataset_id=?" if dataset_id else ""
        by_group_coded = conn.execute(
            f"""SELECT s.group_name AS group_name, COUNT(DISTINCT c.segment_id) AS n
                FROM codes c JOIN segments s ON s.segment_id = c.segment_id
                {group_coded_filter} {"AND" if group_coded_filter else "WHERE"} c.deleted_at IS NULL
                GROUP BY s.group_name""",
            seg_params,
        ).fetchall()

    coded_by_group = {r["group_name"]: r["n"] for r in by_group_coded}
    by_group = {
        r["group_name"]: {"total_segments": r["n"], "coded_segments": coded_by_group.get(r["group_name"], 0)}
        for r in by_group_total
    }
    by_theme = {
        r["theme_id"]: {"name": r["name"], "count": r["n"]} for r in by_theme_rows
    }
    return {
        "total_segments": total_segments,
        "coded_segments": coded_segments,
        "by_theme": by_theme,
        "by_group": by_group,
    }
