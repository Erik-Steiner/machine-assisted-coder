"""Phase 2 baseline classifier: TF-IDF features (shared across all themes,
fit once on the full segment corpus) + one-vs-rest LogisticRegression per
theme, calibrated so scores are usable as probabilities for ranking a review
queue. See scripts/train_classifiers.py for the CLI entry point and
DEVELOPMENT.md's Coding subsystem section for the overall design.

v1 baseline only: TF-IDF, not embeddings (a v2 swap to sentence-transformer
embeddings is Phase 4, once the pipeline below is proven). Codes from any
coder are pooled as positives; every other segment -- explicit negatives and
simply-unlabeled alike -- is treated as a negative. That's a standard
simplification for weak supervision at this label volume (a few dozen to a
few hundred positives against tens of thousands of segments), not a
statement that unlabeled segments truly lack the theme.

Known simplification: metrics come from cross-validated out-of-fold scores
only. The coding plan calls for an additional frozen, never-trained-on
validation slice once label volume is high enough to afford holding data out
of both CV and the final fit -- at the current few-dozen-positives scale,
carving out a slice would starve CV instead of adding rigor, so it's
deferred until themes clear the TRUST_POSITIVES threshold below.

build_corpus() also excludes non-canonical near-duplicate segments by
default once scripts/find_duplicates.py has been run -- e.g. the same
speech independently re-transcribed by several outlets, which would
otherwise replicate a term's IDF weight and a coded label's apparent
support across near-identical text, and leak across CV folds (a
duplicate's twin in the "held-out" fold isn't really held out). See
build_corpus()'s own docstring and coding_store.py's duplicate_* tables.
"""
from datetime import datetime, timezone

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, precision_recall_curve
from sklearn.model_selection import StratifiedKFold, cross_val_predict

import coding_store

TOP_TERMS_N = 20

MIN_POSITIVES_ATTEMPT = 25  # floor to attempt a first classifier at all
TRUST_POSITIVES = 150  # rule-of-thumb floor before trusting the ranking
CV_FOLDS = 5

# Named so the same values feed both the real fit calls below and the
# params logged to coding_store.model_run_params (Web Appendix tab) --
# otherwise a logged params dict could silently drift from what actually ran.
NGRAM_RANGE = (1, 2)
MAX_FEATURES = 5000
MIN_DF = 2
STOP_WORDS = "english"
LOGREG_CLASS_WEIGHT = "balanced"
LOGREG_MAX_ITER = 1000
CALIBRATION_METHOD = "sigmoid"
CALIBRATION_CV = 3

# Segments excluded from build_corpus() below -- and therefore from training,
# predictions, and top-term coefficients entirely -- but never hidden from
# Browse/Coding. speaker_role='interviewer' keeps an interview's questions
# out of topic modeling (see IMPORTING_DATA.md); MIN_SEGMENT_WORDS drops
# fragments too short to carry meaningful topic signal (motivated by Reddit
# threads, where a real share of comments are one-liners like "This.").
MIN_SEGMENT_WORDS = 5


def training_params(duplicate_run_id=None):
    """The hyperparameters actually in force for a training pass, as a plain
    dict -- persisted once per pass via coding_store.save_model_run_params so
    the Web Appendix tab can show exactly how a given model_version's
    predictions were produced. duplicate_run_id (see build_corpus()) records
    which near-duplicate detection run, if any, shaped the training corpus --
    None if no scripts/find_duplicates.py run exists yet, or exclusion was
    explicitly skipped."""
    return {
        "ngram_range": list(NGRAM_RANGE), "max_features": MAX_FEATURES, "min_df": MIN_DF,
        "stop_words": STOP_WORDS, "min_positives_attempt": MIN_POSITIVES_ATTEMPT,
        "cv_folds": CV_FOLDS, "logreg_class_weight": LOGREG_CLASS_WEIGHT,
        "logreg_max_iter": LOGREG_MAX_ITER, "calibration_method": CALIBRATION_METHOD,
        "calibration_cv": CALIBRATION_CV, "duplicate_run_id": duplicate_run_id,
    }


def build_corpus(exclude_duplicates=True):
    """Trainable segments in coding.db, as parallel (segment_ids, texts) lists, in
    a fixed order shared by every theme's feature matrix and prediction rows.
    Excludes interviewer turns and sub-MIN_SEGMENT_WORDS fragments -- see the
    module-level comment above. `!= 'interviewer'` alone would silently drop
    every segment with no speaker_role at all (NULL != 'interviewer' is NULL,
    not true, in SQL) -- the explicit IS NULL OR is required, not stylistic.

    exclude_duplicates=True (the default) also excludes every segment
    belonging to a non-canonical member of a near-duplicate cluster -- e.g.
    the same speech independently re-transcribed by several outlets, which
    would otherwise skew term weighting and inflate apparent label support
    for whatever theme(s) it happens to be coded under. This is a no-op
    (behaves exactly like exclude_duplicates=False) until
    scripts/find_duplicates.py has been run at least once -- safe by
    construction, not by requiring an opt-in flag. Excludes a non-canonical
    duplicate's segments even if they carry active codes: those codes stay
    fully intact in coding.db/Browse/Coding/Review, just not consulted here
    -- see scripts/find_duplicates.py's module docstring for why silently
    keeping them in training would defeat the point of this exclusion.

    Also honors coding_store.duplicate_overrides -- a researcher's manual
    correction always wins over the automated run: an 'exclude' override
    keeps a segment in even if the run flagged it non-canonical (a false
    positive), and an 'include' override excludes a segment even if the run
    never flagged it at all, or flagged it canonical (a false negative).
    Overrides apply even with no run yet -- see coding_store's schema
    comment on duplicate_overrides."""
    run_id = coding_store.get_latest_duplicate_run_id() if exclude_duplicates else None
    dup_clause = ""
    params = [MIN_SEGMENT_WORDS]
    if exclude_duplicates:
        auto_clause = ""
        if run_id:
            auto_clause = """
                OR (
                    EXISTS (
                        SELECT 1 FROM duplicate_cluster_items d
                        WHERE d.run_id = ? AND d.dataset_id = s.dataset_id
                          AND d.item_id = s.item_id AND d.is_canonical = 0
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM duplicate_overrides o
                        WHERE o.dataset_id = s.dataset_id AND o.item_id = s.item_id AND o.action = 'exclude'
                    )
                )"""
            params.append(run_id)
        dup_clause = f"""
            AND NOT (
                EXISTS (
                    SELECT 1 FROM duplicate_overrides o
                    WHERE o.dataset_id = s.dataset_id AND o.item_id = s.item_id AND o.action = 'include'
                )
                {auto_clause}
            )"""
    with coding_store.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT segment_id, text FROM segments s
                WHERE (speaker_role IS NULL OR speaker_role != 'interviewer')
                AND (word_count IS NULL OR word_count >= ?) {dup_clause}
                ORDER BY segment_id""",
            params,
        ).fetchall()
    segment_ids = [r["segment_id"] for r in rows]
    texts = [r["text"] for r in rows]
    return segment_ids, texts, run_id


def fit_vectorizer(texts):
    vectorizer = TfidfVectorizer(
        ngram_range=NGRAM_RANGE, max_features=MAX_FEATURES, min_df=MIN_DF, stop_words=STOP_WORDS,
    )
    X = vectorizer.fit_transform(texts)
    return vectorizer, X


def build_labels(theme_id, segment_ids):
    """0/1 label per segment_id: 1 if it has an active code for theme_id (from
    any coder), 0 otherwise. Returns (y, n_pos)."""
    with coding_store.get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT segment_id FROM codes WHERE theme_id=? AND deleted_at IS NULL",
            (theme_id,),
        ).fetchall()
    positive_ids = {r["segment_id"] for r in rows}
    y = np.array([1 if sid in positive_ids else 0 for sid in segment_ids], dtype=int)
    return y, int(y.sum())


def _best_threshold(y_true, y_score):
    """F1-maximizing point on the precision-recall curve."""
    precision, recall, thresholds = precision_recall_curve(y_true, y_score)
    if len(thresholds) == 0:
        return 0.5, 0.0, 0.0, 0.0
    f1 = np.divide(
        2 * precision * recall, precision + recall,
        out=np.zeros_like(precision), where=(precision + recall) > 0,
    )
    best_idx = int(np.argmax(f1[:-1]))  # last point has no matching threshold
    return float(thresholds[best_idx]), float(precision[best_idx]), float(recall[best_idx]), float(f1[best_idx])


def _make_pipeline():
    return CalibratedClassifierCV(
        estimator=LogisticRegression(class_weight=LOGREG_CLASS_WEIGHT, max_iter=LOGREG_MAX_ITER),
        method=CALIBRATION_METHOD,
        cv=CALIBRATION_CV,
    )


def train_theme(X, y, n_pos):
    """Cross-validate for metrics, then fit a final model on all labeled data.
    Returns (metrics_dict, fitted_final_model), or None if n_pos is below
    MIN_POSITIVES_ATTEMPT."""
    if n_pos < MIN_POSITIVES_ATTEMPT:
        return None

    cv = StratifiedKFold(n_splits=CV_FOLDS, shuffle=True, random_state=0)
    oof_proba = cross_val_predict(_make_pipeline(), X, y, cv=cv, method="predict_proba")[:, 1]

    pr_auc = float(average_precision_score(y, oof_proba))
    threshold, precision, recall, f1 = _best_threshold(y, oof_proba)

    final_model = _make_pipeline()
    final_model.fit(X, y)

    metrics = {
        "n_pos": int(n_pos),
        "n_neg": int(len(y) - n_pos),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "pr_auc": pr_auc,
        "threshold": threshold,
    }
    return metrics, final_model


def top_terms(model, vectorizer, top_n=TOP_TERMS_N):
    """Average the fold-fitted LogisticRegression coefficients inside a fitted
    CalibratedClassifierCV (cv=3 -> 3 folds, each exposing
    calibrated_classifiers_[i].estimator.coef_) into one coefficient vector,
    pair with the shared vectorizer's feature names, and return
    (top_positive, top_negative) as [(term, weight), ...] lists -- the words
    pushing a segment toward vs. away from the theme."""
    coefs = np.array([cc.estimator.coef_[0] for cc in model.calibrated_classifiers_])
    avg_coef = coefs.mean(axis=0)
    features = vectorizer.get_feature_names_out()
    order = np.argsort(avg_coef)
    top_pos = [(str(features[i]), float(avg_coef[i])) for i in order[::-1][:top_n]]
    top_neg = [(str(features[i]), float(avg_coef[i])) for i in order[:top_n]]
    return top_pos, top_neg


def run_training_pass(progress_callback=None, exclude_duplicates=True):
    """One full training pass: build the corpus, fit one shared TF-IDF
    vectorizer, then attempt every active theme. progress_callback(event)
    fires at each stage transition (corpus, vectorizing, theme_start,
    theme_skipped, theme_done) so a caller -- the CLI script or a web
    background job -- can report live progress without duplicating this loop.
    exclude_duplicates is passed straight through to build_corpus() -- see
    its docstring; scripts/train_classifiers.py's --include-duplicates flag
    is this parameter's only caller-facing opt-out.

    Persists model_runs, predictions, and model_top_terms for every theme
    that trains -- the only side effects. Returns:
      {model_version, trained_at, themes_total,
       results: [{theme_id, name, status: "trained"|"skipped_insufficient_positives",
                  n_pos, metrics: {...} | None}, ...],
       error: str | None}
    model_version is None only when error is set (nothing to train on).
    """
    def emit(event):
        if progress_callback:
            progress_callback(event)

    coding_store.init_db()
    themes = coding_store.list_themes()
    if not themes:
        return {"model_version": None, "trained_at": None, "themes_total": 0,
                "results": [], "error": "No themes in the codebook yet."}

    emit({"stage": "corpus"})
    segment_ids, texts, duplicate_run_id = build_corpus(exclude_duplicates=exclude_duplicates)
    if not segment_ids:
        return {"model_version": None, "trained_at": None, "themes_total": len(themes),
                "results": [],
                "error": "No segments in coding.db -- run scripts/build_segments.py first."}

    emit({"stage": "vectorizing", "n_segments": len(segment_ids)})
    vectorizer, X = fit_vectorizer(texts)

    model_version = "tfidf_v1_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    trained_at = datetime.now(timezone.utc).isoformat()
    coding_store.save_model_run_params(model_version, training_params(duplicate_run_id), trained_at)

    results = []
    for i, theme in enumerate(themes):
        emit({"stage": "theme_start", "index": i, "total": len(themes),
              "theme_id": theme["theme_id"], "name": theme["name"]})
        y, n_pos = build_labels(theme["theme_id"], segment_ids)

        trained = train_theme(X, y, n_pos)
        if trained is None:
            results.append({
                "theme_id": theme["theme_id"], "name": theme["name"],
                "status": "skipped_insufficient_positives", "n_pos": n_pos, "metrics": None,
            })
            emit({"stage": "theme_skipped", "index": i, "total": len(themes),
                  "theme_id": theme["theme_id"], "name": theme["name"], "n_pos": n_pos})
            continue

        metrics, model = trained
        coding_store.save_model_run(model_version, theme["theme_id"], trained_at, metrics)

        scores = model.predict_proba(X)[:, 1]
        coding_store.save_predictions(model_version, theme["theme_id"], segment_ids, scores)

        top_pos, top_neg = top_terms(model, vectorizer)
        coding_store.save_top_terms(model_version, theme["theme_id"], top_pos, top_neg)

        results.append({
            "theme_id": theme["theme_id"], "name": theme["name"],
            "status": "trained", "n_pos": n_pos, "metrics": metrics,
        })
        emit({"stage": "theme_done", "index": i, "total": len(themes),
              "theme_id": theme["theme_id"], "name": theme["name"], "metrics": metrics})

    return {"model_version": model_version, "trained_at": trained_at,
            "themes_total": len(themes), "results": results, "error": None}
