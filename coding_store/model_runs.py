"""Model tab storage: one classifier run per (model_version, theme_id) --
metrics, predictions, and top predictive terms. Read-only monitoring data;
nothing here mutates codes/themes. Depends only on schema and labels (the
effective-label rules behind its already_coded/already_rejected flags and
coded counts).
"""
from . import labels, schema

# Which engine produced a run is encoded only in its model_version prefix (see
# classifier.run_training_pass()/run_embedding_training_pass()). This dict is the one
# place that mapping lives -- topic_model_export.py and the API's `engine` parameters
# all go through it, and model.js's engineOfVersion() mirrors it on the frontend.
ENGINE_PREFIXES = {"tfidf": "tfidf_v1_", "embed": "embed_v1_"}
# Display names, served with the prefixes by engine_registry() (GET /api/engines) so the UI
# never hard-codes the list (CODEBOOK_PLAN L0). "Embedding classifier", not "Embedding": a
# later embedding-clustering topic method must not be confused with it.
ENGINE_LABELS = {"tfidf": "TF-IDF", "embed": "Embedding classifier"}


def engine_registry():
    """Every score engine, in display order: [{id, label, prefix, kind}]. kind is
    "classifier" for both today; CODEBOOK_PLAN L0's topic methods will be listed beside
    these, not mixed in."""
    return [{"id": engine, "label": ENGINE_LABELS[engine], "prefix": prefix, "kind": "classifier"}
            for engine, prefix in ENGINE_PREFIXES.items()]


def engine_of_version(model_version):
    """"tfidf" | "embed" for a known model_version prefix, None otherwise."""
    for engine, prefix in ENGINE_PREFIXES.items():
        if (model_version or "").startswith(prefix):
            return engine
    return None


def _engine_clause(engine, column="model_version"):
    """(sql, params) restricting `column` to one engine's model_versions -- or ("", ())
    for engine=None. substr() rather than LIKE because the prefixes contain `_`,
    which LIKE treats as a wildcard. Raises ValueError for an unknown engine."""
    if engine is None:
        return "", ()
    prefix = ENGINE_PREFIXES.get(engine)
    if prefix is None:
        raise ValueError(f"unknown engine '{engine}' -- expected one of {sorted(ENGINE_PREFIXES)}")
    return f"substr({column}, 1, ?) = ?", (len(prefix), prefix)


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


def _with_engine(row):
    d = dict(row)
    d["engine"] = engine_of_version(d["model_version"])
    return d


def list_model_runs(theme_id=None):
    """Run rows newest first, each with `engine` and `negative_mode` (what counted
    as a negative -- see classifier.NEGATIVE_MODES, read from the pass's
    model_run_params). Runs from before negative modes existed have none
    recorded; they all treated every uncoded segment as a negative, so they read
    as "all_uncoded"."""
    where = "WHERE m.theme_id=?" if theme_id else ""
    params = (theme_id,) if theme_id else ()
    with schema.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT m.*, json_extract(pr.params_json, '$.negative_mode') AS negative_mode
                FROM model_runs m LEFT JOIN model_run_params pr ON pr.model_version = m.model_version
                {where} ORDER BY m.trained_at DESC""",
            params,
        ).fetchall()
    out = []
    for r in rows:
        d = _with_engine(r)
        d["negative_mode"] = d["negative_mode"] or "all_uncoded"
        out.append(d)
    return out


def get_latest_model_runs(engine=None):
    """{theme_id: latest model_run dict}, one row per theme (its most recent run),
    each carrying an `engine` key. engine ("tfidf"/"embed") restricts to that
    engine's runs. Pass it whenever the result decides whose *scores* get shown:
    with engine=None, an embedding run silently becomes "latest" for the themes it
    trained, so one view ends up mixing two engines whose scores aren't comparable.
    engine=None stays correct for engine-independent facts like n_pos (see
    classifier.embedding_readiness())."""
    inner_clause, params = _engine_clause(engine)
    outer_clause, _ = _engine_clause(engine, column="m.model_version")
    inner_where = f"WHERE {inner_clause}" if inner_clause else ""
    # The outer filter matters too: two engines' runs could share a trained_at.
    outer_where = f"WHERE {outer_clause}" if outer_clause else ""
    with schema.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT m.* FROM model_runs m
                INNER JOIN (
                  SELECT theme_id, MAX(trained_at) AS max_trained_at
                  FROM model_runs {inner_where} GROUP BY theme_id
                ) latest ON latest.theme_id = m.theme_id AND latest.max_trained_at = m.trained_at
                {outer_where}""",
            params + params,
        ).fetchall()
    return {r["theme_id"]: _with_engine(r) for r in rows}


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


def _select_predictions(theme_id, model_version, extra_where="", extra_params=(), tail="", tail_params=()):
    """Shared read behind get_top_predictions()/get_predictions_for_theme():
    prediction rows joined with segment text, each flagged already_coded /
    already_rejected under the effective-label rules (labels.effective_label_sql():
    a code on a sub-code counts for the parent; a rejection of an ancestor counts;
    a positive wins)."""
    coded_sql, coded_params, rejected_sql, rejected_params = labels.effective_label_sql(theme_id)
    with schema.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT p.segment_id, p.score, p.oof_score, s.item_id, s.dataset_id, s.group_name,
                      s.person_name, s.speaker_name, s.speaker_role, s.depth,
                      s.word_count, s.timestamp, s.text,
                      {coded_sql} AS already_coded,
                      {rejected_sql} AS already_rejected
               FROM predictions p JOIN segments s ON s.segment_id = p.segment_id
               WHERE p.theme_id = ? AND p.model_version = ? {extra_where}
               ORDER BY p.score DESC {tail}""",
            (*coded_params, *rejected_params, theme_id, model_version, *extra_params, *tail_params),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["item_id"] = str(d["item_id"])
        out.append(d)
    return out


def get_scores_for_segments(segment_ids, theme_ids, engine="tfidf"):
    """{segment_id: {theme_id: {"score", "threshold", "model_version"}}} -- each
    theme's score for each given segment from that theme's latest run of `engine`
    (see get_latest_model_runs()). Themes without such a run, and segments a run
    didn't score, are simply absent. Backs the Review cards' per-theme score bars,
    so it's batched for a page of segments, not a whole corpus."""
    latest = get_latest_model_runs(engine=engine)
    out = {}
    if not segment_ids:
        return out
    batch_size = schema.SQLITE_MAX_VARIABLES - 2  # room for theme_id + model_version
    with schema.get_conn() as conn:
        for theme_id in theme_ids:
            run = latest.get(theme_id)
            if run is None:
                continue
            for i in range(0, len(segment_ids), batch_size):
                batch = segment_ids[i:i + batch_size]
                placeholders = ",".join("?" * len(batch))
                rows = conn.execute(
                    f"""SELECT segment_id, score FROM predictions
                        WHERE theme_id=? AND model_version=? AND segment_id IN ({placeholders})""",
                    (theme_id, run["model_version"], *batch),
                ).fetchall()
                for r in rows:
                    out.setdefault(r["segment_id"], {})[theme_id] = {
                        "score": r["score"], "threshold": run["threshold"], "model_version": run["model_version"],
                    }
    return out


def get_top_predictions(theme_id, model_version, limit=15):
    """Top-scored segments for one theme's model run, joined with segment text
    and flagged for whether they're already coded/rejected -- exemplars for the
    Model tab's interpret panel, and the Review queue's model-flagged list."""
    return _select_predictions(theme_id, model_version, tail="LIMIT ?", tail_params=(limit,))


def get_predictions_for_theme(theme_id, model_version, threshold=None):
    """Every segment this theme/model_version was scored against -- the
    full-corpus counterpart to get_top_predictions() above (same join/shape,
    no LIMIT, and an optional score threshold instead of a rank cutoff). This
    is the read behind the full-corpus topic-model export; the Review queue
    and Model tab exemplar panel keep using get_top_predictions() unchanged."""
    if threshold is None:
        return _select_predictions(theme_id, model_version)
    return _select_predictions(theme_id, model_version, "AND p.score >= ?", (threshold,))


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
    """coded_count vs. flagged_uncoded_count (segments the model scores above
    threshold for this run that aren't already coded, and that no coder has
    explicitly rejected for this theme) -- the headline "the model found N more
    cases" stat. A rejected segment was looked at and judged not to be this
    theme, so counting it as a "more case" (or toward coverage, see
    get_corpus_coverage()) would overstate the theme. All three counts use the
    effective-label rules (see coding_store/labels.py): for a parent theme,
    coded_count includes segments coded only with a sub-code, matching the n_pos
    the classifier trained on."""
    coded_sql, coded_params, rejected_sql, rejected_params = labels.effective_label_sql(theme_id)
    with schema.get_conn() as conn:
        flagged_uncoded_count = conn.execute(
            f"""SELECT COUNT(*) AS n FROM predictions p
               WHERE p.theme_id=? AND p.model_version=? AND p.score >= ?
               AND NOT {coded_sql} AND NOT {rejected_sql}""",
            (theme_id, model_version, threshold, *coded_params, *rejected_params),
        ).fetchone()["n"]
    return {
        "coded_count": len(labels.effective_positive_segment_ids(theme_id)),
        "flagged_uncoded_count": flagged_uncoded_count,
        "rejected_count": len(labels.effective_rejected_segment_ids(theme_id)),
        "threshold": threshold,
    }
