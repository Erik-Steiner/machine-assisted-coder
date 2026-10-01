"""Model tab storage: one classifier run per (model_version, theme_id) --
metrics, predictions, and top predictive terms. Read-only monitoring data;
nothing here mutates codes/themes. No dependency on any other coding_store
submodule.
"""
from . import schema


def save_model_run(model_version, theme_id, trained_at, metrics):
    with schema.get_conn() as conn:
        conn.execute(
            """INSERT INTO model_runs
                 (model_version, theme_id, trained_at, n_pos, n_neg, precision, recall, f1, pr_auc, threshold)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(model_version, theme_id) DO UPDATE SET
                 trained_at=excluded.trained_at, n_pos=excluded.n_pos, n_neg=excluded.n_neg,
                 precision=excluded.precision, recall=excluded.recall, f1=excluded.f1,
                 pr_auc=excluded.pr_auc, threshold=excluded.threshold""",
            (model_version, theme_id, trained_at, metrics["n_pos"], metrics["n_neg"],
             metrics["precision"], metrics["recall"], metrics["f1"], metrics["pr_auc"],
             metrics["threshold"]),
        )


def save_predictions(model_version, theme_id, segment_ids, scores, oof_scores=None):
    """scores: the final model's score per segment. oof_scores (optional, same
    order/length): each segment's out-of-fold CV score -- see
    classifier.train_theme(). Stored as NULL when not given (e.g. an engine or
    import that has no CV pass), which readers treat as "no honest score
    available", never as 0."""
    now = schema._now()
    if oof_scores is None:
        oof_scores = [None] * len(segment_ids)
    rows = [
        (sid, theme_id, model_version, float(score), None if oof is None else float(oof), now)
        for sid, score, oof in zip(segment_ids, scores, oof_scores)
    ]
    with schema.get_conn() as conn:
        conn.executemany(
            """INSERT INTO predictions (segment_id, theme_id, model_version, score, oof_score, created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(segment_id, theme_id, model_version) DO UPDATE SET
                 score=excluded.score, oof_score=excluded.oof_score, created_at=excluded.created_at""",
            rows,
        )


def list_model_runs(theme_id=None):
    with schema.get_conn() as conn:
        if theme_id:
            rows = conn.execute(
                "SELECT * FROM model_runs WHERE theme_id=? ORDER BY trained_at DESC", (theme_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM model_runs ORDER BY trained_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_latest_model_runs():
    """{theme_id: latest model_run dict}, one row per theme (its most recent run)."""
    with schema.get_conn() as conn:
        rows = conn.execute(
            """SELECT m.* FROM model_runs m
               INNER JOIN (
                 SELECT theme_id, MAX(trained_at) AS max_trained_at
                 FROM model_runs GROUP BY theme_id
               ) latest ON latest.theme_id = m.theme_id AND latest.max_trained_at = m.trained_at"""
        ).fetchall()
    return {r["theme_id"]: dict(r) for r in rows}


def save_top_terms(model_version, theme_id, top_pos, top_neg):
    """top_pos/top_neg: [(term, weight), ...], already ranked best-first."""
    rows = []
    for rank, (term, weight) in enumerate(top_pos, start=1):
        rows.append((model_version, theme_id, "positive", rank, term, weight))
    for rank, (term, weight) in enumerate(top_neg, start=1):
        rows.append((model_version, theme_id, "negative", rank, term, weight))
    with schema.get_conn() as conn:
        conn.execute(
            "DELETE FROM model_top_terms WHERE model_version=? AND theme_id=?",
            (model_version, theme_id),
        )
        conn.executemany(
            """INSERT INTO model_top_terms (model_version, theme_id, direction, rank, term, weight)
               VALUES (?,?,?,?,?,?)""",
            rows,
        )


def get_top_terms(model_version, theme_id):
    with schema.get_conn() as conn:
        rows = conn.execute(
            """SELECT direction, term, weight FROM model_top_terms
               WHERE model_version=? AND theme_id=? ORDER BY direction, rank""",
            (model_version, theme_id),
        ).fetchall()
    positive = [{"term": r["term"], "weight": r["weight"]} for r in rows if r["direction"] == "positive"]
    negative = [{"term": r["term"], "weight": r["weight"]} for r in rows if r["direction"] == "negative"]
    return {"positive": positive, "negative": negative}


def get_top_predictions(theme_id, model_version, limit=15):
    """Top-scored segments for one theme's model run, joined with segment text
    and flagged for whether they're already coded -- exemplars for the
    Model tab's interpret panel."""
    with schema.get_conn() as conn:
        rows = conn.execute(
            """SELECT p.segment_id, p.score, p.oof_score, s.item_id, s.dataset_id, s.group_name,
                      s.person_name, s.speaker_name, s.speaker_role, s.depth,
                      s.word_count, s.timestamp, s.text,
                      EXISTS(
                        SELECT 1 FROM codes c WHERE c.segment_id = p.segment_id
                        AND c.theme_id = p.theme_id AND c.deleted_at IS NULL
                      ) AS already_coded
               FROM predictions p JOIN segments s ON s.segment_id = p.segment_id
               WHERE p.theme_id = ? AND p.model_version = ?
               ORDER BY p.score DESC LIMIT ?""",
            (theme_id, model_version, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["item_id"] = str(d["item_id"])
        out.append(d)
    return out


def get_predictions_for_theme(theme_id, model_version, threshold=None):
    """Every segment this theme/model_version was scored against -- the
    full-corpus counterpart to get_top_predictions() above (same join/shape,
    no LIMIT, and an optional score threshold instead of a rank cutoff). This
    is the read behind the full-corpus topic-model export; the Review queue
    and Model tab exemplar panel keep using get_top_predictions() unchanged."""
    where = "p.theme_id = ? AND p.model_version = ?"
    params = [theme_id, model_version]
    if threshold is not None:
        where += " AND p.score >= ?"
        params.append(threshold)
    with schema.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT p.segment_id, p.score, p.oof_score, s.item_id, s.dataset_id, s.group_name,
                      s.person_name, s.speaker_name, s.speaker_role, s.depth,
                      s.word_count, s.timestamp, s.text,
                      EXISTS(
                        SELECT 1 FROM codes c WHERE c.segment_id = p.segment_id
                        AND c.theme_id = p.theme_id AND c.deleted_at IS NULL
                      ) AS already_coded
               FROM predictions p JOIN segments s ON s.segment_id = p.segment_id
               WHERE {where}
               ORDER BY p.score DESC""",
            params,
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["item_id"] = str(d["item_id"])
        out.append(d)
    return out


def get_corpus_coverage(theme_id, model_version, threshold, total_segments):
    """Full-corpus prevalence at threshold: coded_count (already coded) +
    flagged_uncoded_count (scores >= threshold but not yet coded) = covered_count,
    against total_segments (the caller's classifier.build_corpus() scope) --
    what fraction of the whole corpus this theme currently covers, not just
    "N more from the top 30." total_segments is passed in rather than computed
    here so this module keeps its existing no-dependency-on-classifier.py shape."""
    value_prop = get_value_prop(theme_id, model_version, threshold)
    covered_count = value_prop["coded_count"] + value_prop["flagged_uncoded_count"]
    coverage_pct = (covered_count / total_segments) if total_segments else 0.0
    return {
        "coded_count": value_prop["coded_count"],
        "flagged_uncoded_count": value_prop["flagged_uncoded_count"],
        "covered_count": covered_count,
        "total_segments": total_segments,
        "coverage_pct": coverage_pct,
    }


def get_value_prop(theme_id, model_version, threshold):
    """coded_count (from codes) vs. flagged_uncoded_count (segments the model
    scores above threshold for this run that aren't already coded) -- the
    headline "the model found N more cases" stat."""
    with schema.get_conn() as conn:
        coded_count = conn.execute(
            "SELECT COUNT(DISTINCT segment_id) AS n FROM codes WHERE theme_id=? AND deleted_at IS NULL",
            (theme_id,),
        ).fetchone()["n"]
        flagged_uncoded_count = conn.execute(
            """SELECT COUNT(*) AS n FROM predictions p
               WHERE p.theme_id=? AND p.model_version=? AND p.score >= ?
               AND NOT EXISTS (
                 SELECT 1 FROM codes c WHERE c.segment_id = p.segment_id
                 AND c.theme_id = p.theme_id AND c.deleted_at IS NULL
               )""",
            (theme_id, model_version, threshold),
        ).fetchone()["n"]
    return {"coded_count": coded_count, "flagged_uncoded_count": flagged_uncoded_count, "threshold": threshold}
