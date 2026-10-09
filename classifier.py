"""Phase 2 baseline classifier: TF-IDF features (shared across all themes,
fit once on the full segment corpus) + one-vs-rest LogisticRegression per
theme, calibrated so scores are usable as probabilities for ranking a review
queue. See scripts/train_classifiers.py for the CLI entry point and
docs/DEVELOPMENT.md's Coding subsystem section for the overall design.

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
import importlib.util
from datetime import datetime, timezone

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, precision_recall_curve
from sklearn.model_selection import StratifiedKFold, cross_val_predict

import codebook_export
from coding_store import activity_log, dataset_status, duplicates, embeddings, labels, model_runs, schema, themes

TOP_TERMS_N = 20

MIN_POSITIVES_ATTEMPT = 25  # floor to attempt a first classifier at all
TRUST_POSITIVES = 150  # rule-of-thumb floor before trusting the ranking
CV_FOLDS = 5

# What counts as a negative example (roadmap A6) -- see training_labels():
#   "reviewed"    (default): only segments the researcher judged -- explicit rejections,
#                 plus uncoded segments in items marked "fully coded" / confirmed "none
#                 of these" after the theme existed (coding_store/labels.py). Unread
#                 segments are left out of fitting and metrics, but still scored.
#   "read_in_full": like "reviewed", but an interview's uncoded segments only count when
#                 its mark is read in full -- confirmed, or made interactively -- not an
#                 unconfirmed bulk mark (CODEBOOK_PLAN K5; coding_store/reviewed.py).
#   "all_uncoded": every uncoded segment in the corpus is a negative (the original
#                 behavior). Simpler, but tells the model that every relevant passage
#                 you haven't read yet is a counter-example, which pushes down exactly
#                 the passages stage 4 is meant to find, and biases its precision.
NEGATIVE_MODES = ("reviewed", "read_in_full", "all_uncoded")
DEFAULT_NEGATIVE_MODE = "reviewed"
# In the "reviewed" and "read_in_full" modes a theme needs this many judged negatives to train; below it the
# theme is skipped (status skipped_insufficient_negatives) rather than trained on a
# handful of negatives -- the researcher can mark more interviews, or train that run
# with "all_uncoded".
MIN_NEGATIVES_REVIEWED = 50

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

# Optional embedding-based ("richer") engine -- see build_embeddings()/
# run_embedding_training_pass() below and requirements-embeddings.txt. Not a
# hyperparameter used by the TF-IDF pass at all; named here alongside it for
# the same reason (single place to look, easy to audit what a model_version
# was actually produced with).
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"

# Segments excluded from build_corpus() below -- and therefore from training,
# predictions, and top-term coefficients entirely -- but never hidden from
# Browse/Coding. speaker_role='interviewer' keeps an interview's questions
# out of topic modeling (see docs/IMPORTING_DATA.md); MIN_SEGMENT_WORDS drops
# fragments too short to carry meaningful topic signal (motivated by Reddit
# threads, where a real share of comments are one-liners like "This.").
MIN_SEGMENT_WORDS = 5


def training_params(duplicate_run_id=None, excluded_dataset_ids=None, scheme_id=None,
                    negative_mode=DEFAULT_NEGATIVE_MODE, codebook_versions=None):
    """The hyperparameters actually in force for a training pass, as a plain
    dict -- persisted once per pass via coding_store.activity_log's
    save_model_run_params so the Web Appendix tab can show exactly how a given
    model_version's predictions were produced. duplicate_run_id (see
    build_corpus()) records which near-duplicate detection run, if any, shaped
    the training corpus -- None if no scripts/find_duplicates.py run exists
    yet, or exclusion was explicitly skipped. excluded_dataset_ids (see
    build_corpus()) records which whole datasets, if any, were flagged
    'excluded'/'unloaded' and therefore left out of the corpus -- see
    coding_store.dataset_status. scheme_id records which coding scheme's
    themes the pass trained (None = every scheme -- see run_training_pass()).
    negative_mode records what counted as a negative (see NEGATIVE_MODES); runs
    from before it existed have no key and were effectively "all_uncoded".
    codebook_versions ({scheme_id: codebook_export.current_codebook_version()}, every
    scheme the pass trained) records which codebook the run was trained under, so the
    Train tab's Runs page can tell whether the codebook has changed since
    (CODEBOOK_PLAN U2); runs from before it existed have no key."""
    return {
        "scheme_id": scheme_id, "roll_up": ROLL_UP, "codebook_versions": codebook_versions or {},
        "negative_mode": negative_mode, "min_negatives_reviewed": MIN_NEGATIVES_REVIEWED,
        "ngram_range": list(NGRAM_RANGE), "max_features": MAX_FEATURES, "min_df": MIN_DF,
        "stop_words": STOP_WORDS, "min_positives_attempt": MIN_POSITIVES_ATTEMPT,
        "cv_folds": CV_FOLDS, "logreg_class_weight": LOGREG_CLASS_WEIGHT,
        "logreg_max_iter": LOGREG_MAX_ITER, "calibration_method": CALIBRATION_METHOD,
        "calibration_cv": CALIBRATION_CV, "duplicate_run_id": duplicate_run_id,
        "excluded_dataset_ids": sorted(excluded_dataset_ids) if excluded_dataset_ids else [],
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
    comment on duplicate_overrides.

    Also always excludes every segment belonging to a whole dataset flagged
    'excluded' or 'unloaded' in coding_store.dataset_status -- a researcher's
    explicit "this corpus isn't for analysis" call from the Coding tab's
    Datasets panel, independent of exclude_duplicates. Returns the set of
    excluded dataset_ids (empty if none flagged) so callers can log it."""
    run_id = duplicates.get_latest_duplicate_run_id() if exclude_duplicates else None
    excluded_datasets = dataset_status.excluded_dataset_ids()
    dataset_clause = ""
    params = [MIN_SEGMENT_WORDS]
    if excluded_datasets:
        placeholders = ",".join("?" * len(excluded_datasets))
        dataset_clause = f" AND s.dataset_id NOT IN ({placeholders})"
        params.extend(sorted(excluded_datasets))
    dup_clause = ""
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
    with schema.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT segment_id, text FROM segments s
                WHERE (speaker_role IS NULL OR speaker_role != 'interviewer')
                AND (word_count IS NULL OR word_count >= ?) {dataset_clause} {dup_clause}
                ORDER BY segment_id""",
            params,
        ).fetchall()
    segment_ids = [r["segment_id"] for r in rows]
    texts = [r["text"] for r in rows]
    return segment_ids, texts, run_id, excluded_datasets


def _no_themes_error(scheme_id):
    if scheme_id:
        return "This scheme has no active themes yet -- add some in the Code tab first."
    return "No themes in the codebook yet."


def fit_vectorizer(texts):
    vectorizer = TfidfVectorizer(
        ngram_range=NGRAM_RANGE, max_features=MAX_FEATURES, min_df=MIN_DF, stop_words=STOP_WORDS,
    )
    X = vectorizer.fit_transform(texts)
    return vectorizer, X


# Recorded in every run's params: positives roll up from sub-codes (see
# coding_store/labels.py). Runs from before this existed have no roll_up key; for
# those, a parent theme's positives were its direct codes only.
ROLL_UP = True


def build_labels(theme_id, segment_ids):
    """0/1 label per segment_id: 1 if it's effectively positive for theme_id --
    an active code (from any coder) on theme_id or any of its sub-codes (see
    coding_store/labels.py) -- 0 otherwise. Returns (y, n_pos). This is the
    "all_uncoded" labeling; training goes through training_labels()."""
    positive_ids = labels.effective_positive_segment_ids(theme_id)
    y = np.array([1 if sid in positive_ids else 0 for sid in segment_ids], dtype=int)
    return y, int(y.sum())


def training_labels(theme_id, segment_ids, negative_mode=DEFAULT_NEGATIVE_MODE):
    """Which rows of the corpus a theme is fitted on, and their labels.
    Returns (row_idx, y, n_pos, n_neg): row_idx indexes into segment_ids (and the
    shared feature matrix), y is 0/1 for exactly those rows. In "all_uncoded" mode
    every row is used; in "reviewed" mode only effectively positive rows and
    labels.reviewed_negative_segment_ids() rows are, so unread segments are
    neither positives nor negatives. Raises ValueError for an unknown mode."""
    if negative_mode == "all_uncoded":
        y, n_pos = build_labels(theme_id, segment_ids)
        return np.arange(len(segment_ids)), y, n_pos, int(len(y) - n_pos)
    if negative_mode not in ("reviewed", "read_in_full"):
        raise ValueError(f"unknown negative_mode '{negative_mode}' -- expected one of {NEGATIVE_MODES}")
    positive_ids = labels.effective_positive_segment_ids(theme_id)
    negative_ids = labels.reviewed_negative_segment_ids(theme_id, read_in_full_only=negative_mode == "read_in_full")
    row_idx, y = [], []
    for i, sid in enumerate(segment_ids):
        if sid in positive_ids:
            row_idx.append(i)
            y.append(1)
        elif sid in negative_ids:
            row_idx.append(i)
            y.append(0)
    y = np.array(y, dtype=int)
    n_pos = int(y.sum())
    return np.array(row_idx, dtype=int), y, n_pos, int(len(y) - n_pos)


def _skip_status(n_pos, n_neg, negative_mode, min_positives):
    """The skipped_* status for a theme that can't be trained yet, or None."""
    if n_pos < min_positives:
        return "skipped_insufficient_positives" if min_positives == MIN_POSITIVES_ATTEMPT \
            else "skipped_below_trust_floor"
    if negative_mode in ("reviewed", "read_in_full") and n_neg < MIN_NEGATIVES_REVIEWED:
        return "skipped_insufficient_negatives"
    return None


def _fit_on_rows(X, row_idx, y, n_pos):
    """train_theme() on just row_idx's rows; returns (metrics, model, oof_full),
    where oof_full has one entry per corpus row -- the out-of-fold score for
    rows that were fitted on, None for the rest (unlabeled in "reviewed" mode)."""
    X_fit = X if len(row_idx) == X.shape[0] else X[row_idx]
    metrics, model, oof = train_theme(X_fit, y, n_pos)
    oof_full = [None] * X.shape[0]
    for j, i in enumerate(row_idx):
        oof_full[i] = float(oof[j])
    return metrics, model, oof_full


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
    Returns (metrics_dict, fitted_final_model, oof_proba), or None if n_pos is
    below MIN_POSITIVES_ATTEMPT. oof_proba is each row's out-of-fold score --
    from a CV fold model that never saw that row -- and is returned (not just
    used for metrics) because it's the only honest score for a segment the
    final model trained on: the final model has effectively memorized its own
    positives, so a miscoded segment still gets a high final-model score."""
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
    return metrics, final_model, oof_proba


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


def run_training_pass(progress_callback=None, exclude_duplicates=True, scheme_id=None,
                      negative_mode=DEFAULT_NEGATIVE_MODE):
    """One full training pass: build the corpus, fit one shared TF-IDF
    vectorizer, then attempt every active theme -- or only scheme_id's active
    themes, when given. Scoping to a scheme changes which themes get a run, not
    any theme's scores: the vectorizer is always fit on the whole corpus, so a
    theme trained alone scores exactly as it would trained alongside every
    other scheme's themes. progress_callback(event)
    fires at each stage transition (corpus, vectorizing, theme_start,
    theme_skipped, theme_done) so a caller -- the CLI script or a web
    background job -- can report live progress without duplicating this loop.
    exclude_duplicates is passed straight through to build_corpus() -- see
    its docstring; scripts/train_classifiers.py's --include-duplicates flag
    is this parameter's only caller-facing opt-out. negative_mode picks what counts
    as a negative example (see NEGATIVE_MODES / training_labels()).

    Persists model_runs, predictions, and model_top_terms for every theme
    that trains -- the only side effects. Every corpus segment is scored, whatever
    the negative_mode. Returns:
      {model_version, trained_at, themes_total,
       results: [{theme_id, name, status: "trained" | "skipped_insufficient_positives"
                  | "skipped_insufficient_negatives", n_pos, n_neg, metrics: {...} | None}, ...],
       error: str | None}
    model_version is None only when error is set (nothing to train on).
    """
    def emit(event):
        if progress_callback:
            progress_callback(event)

    if negative_mode not in NEGATIVE_MODES:
        raise ValueError(f"unknown negative_mode '{negative_mode}' -- expected one of {NEGATIVE_MODES}")
    schema.init_db()
    theme_list = themes.list_themes(scheme_id=scheme_id)
    if not theme_list:
        return {"model_version": None, "trained_at": None, "themes_total": 0,
                "results": [], "error": _no_themes_error(scheme_id)}

    emit({"stage": "corpus"})
    segment_ids, texts, duplicate_run_id, excluded_dataset_ids = build_corpus(
        exclude_duplicates=exclude_duplicates
    )
    if not segment_ids:
        return {"model_version": None, "trained_at": None, "themes_total": len(theme_list),
                "results": [],
                "error": "No segments in coding.db -- run scripts/build_segments.py first."}

    emit({"stage": "vectorizing", "n_segments": len(segment_ids)})
    vectorizer, X = fit_vectorizer(texts)

    model_version = "tfidf_v1_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    trained_at = datetime.now(timezone.utc).isoformat()
    activity_log.save_model_run_params(
        model_version,
        training_params(duplicate_run_id, excluded_dataset_ids, scheme_id, negative_mode,
                        codebook_export.codebook_versions(scheme_id)),
        trained_at,
    )

    results = []
    for i, theme in enumerate(theme_list):
        emit({"stage": "theme_start", "index": i, "total": len(theme_list),
              "theme_id": theme["theme_id"], "name": theme["name"]})
        row_idx, y, n_pos, n_neg = training_labels(theme["theme_id"], segment_ids, negative_mode)

        skip = _skip_status(n_pos, n_neg, negative_mode, MIN_POSITIVES_ATTEMPT)
        if skip:
            results.append({
                "theme_id": theme["theme_id"], "name": theme["name"],
                "status": skip, "n_pos": n_pos, "n_neg": n_neg, "metrics": None,
            })
            emit({"stage": "theme_skipped", "index": i, "total": len(theme_list),
                  "theme_id": theme["theme_id"], "name": theme["name"],
                  "status": skip, "n_pos": n_pos, "n_neg": n_neg})
            continue

        metrics, model, oof_scores = _fit_on_rows(X, row_idx, y, n_pos)
        model_runs.save_model_run(model_version, theme["theme_id"], trained_at, metrics)

        scores = model.predict_proba(X)[:, 1]
        model_runs.save_predictions(model_version, theme["theme_id"], segment_ids, scores, oof_scores)

        top_pos, top_neg = top_terms(model, vectorizer)
        model_runs.save_top_terms(model_version, theme["theme_id"], top_pos, top_neg)

        results.append({
            "theme_id": theme["theme_id"], "name": theme["name"],
            "status": "trained", "n_pos": n_pos, "n_neg": n_neg, "metrics": metrics,
        })
        emit({"stage": "theme_done", "index": i, "total": len(theme_list),
              "theme_id": theme["theme_id"], "name": theme["name"], "metrics": metrics})

    return {"model_version": model_version, "trained_at": trained_at,
            "themes_total": len(theme_list), "results": results, "error": None}


# --- Optional embedding-based ("richer") engine --------------------------------------
#
# The TF-IDF pass above's core training functions are already feature-matrix-agnostic --
# train_theme(X, y, n_pos) just needs a matrix and a label vector, and build_labels()/
# _best_threshold() don't care what produced X either. So "bootstrap a richer,
# embedding-based classifier" is: reuse that exact same supervised pipeline, on the exact
# same codes-table labels, just swapping the feature matrix from TF-IDF to sentence
# embeddings -- not a new modeling approach. Gated behind embedding_readiness() so it's
# only offered once (a) sentence-transformers is actually installed (see
# requirements-embeddings.txt -- optional, never part of the default install) and (b) the
# codebook has enough human-verified volume to be worth bootstrapping from (TRUST_POSITIVES,
# not the lower MIN_POSITIVES_ATTEMPT the TF-IDF pass uses).

def embedding_readiness(scheme_id=None):
    """Whether the embedding engine can be used right now: installed (the
    optional sentence-transformers package is importable) and volume_ready
    (at least one theme's latest run -- either engine, n_pos is
    engine-independent -- has cleared TRUST_POSITIVES). Both surfaced
    separately so the UI can say exactly what's missing rather than showing
    one opaque locked button."""
    installed = importlib.util.find_spec("sentence_transformers") is not None
    theme_list = themes.list_themes(scheme_id=scheme_id)
    latest = model_runs.get_latest_model_runs()
    meeting = [
        t for t in theme_list
        if latest.get(t["theme_id"], {}).get("n_pos", 0) >= TRUST_POSITIVES
    ]
    return {
        "installed": installed,
        "volume_ready": bool(meeting),
        "ready": installed and bool(meeting),
        "themes_meeting_trust": [{"theme_id": t["theme_id"], "name": t["name"]} for t in meeting],
        "themes_total_active": len(theme_list),
        "trust_positives": TRUST_POSITIVES,
    }


def embedding_training_params(model_name=EMBEDDING_MODEL_NAME, scheme_id=None,
                              negative_mode=DEFAULT_NEGATIVE_MODE, codebook_versions=None):
    """Mirrors training_params() -- persisted once per embedding training
    pass so the Web Appendix tab shows exactly how a given embed_v1_*
    model_version was produced."""
    return {
        "scheme_id": scheme_id, "roll_up": ROLL_UP, "codebook_versions": codebook_versions or {},
        "negative_mode": negative_mode, "min_negatives_reviewed": MIN_NEGATIVES_REVIEWED,
        "engine": "embed_v1", "embedding_model": model_name, "trust_positives_floor": TRUST_POSITIVES,
        "cv_folds": CV_FOLDS, "logreg_class_weight": LOGREG_CLASS_WEIGHT,
        "logreg_max_iter": LOGREG_MAX_ITER, "calibration_method": CALIBRATION_METHOD,
        "calibration_cv": CALIBRATION_CV,
    }


def build_embeddings(segment_ids, texts, model_name=EMBEDDING_MODEL_NAME):
    """Sentence embeddings for segment_ids/texts, in that exact order,
    cached via coding_store.embeddings/paths.EMBEDDINGS_DIR so repeated
    training passes don't re-embed unchanged text -- re-embedding the whole
    corpus is the expensive step, and it shouldn't repeat just because the
    codebook grew. The only place in this module that imports
    sentence_transformers, and only when actually called -- everything else
    here works identically whether or not the optional package is installed."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise ImportError(
            "sentence-transformers isn't installed -- run "
            "`pip install -r requirements-embeddings.txt` to enable the embedding-based topic model."
        ) from exc

    missing_ids, missing_texts = embeddings.filter_missing(segment_ids, texts, model_name)
    if missing_ids:
        model = SentenceTransformer(model_name)
        vectors = model.encode(missing_texts, show_progress_bar=False, convert_to_numpy=True)
        embeddings.append_embeddings(model_name, missing_ids, vectors)
    return embeddings.load_matrix(segment_ids, model_name)


def run_embedding_training_pass(progress_callback=None, exclude_duplicates=True, scheme_id=None,
                                negative_mode=DEFAULT_NEGATIVE_MODE):
    """Embedding-engine counterpart to run_training_pass(): same shape and
    same side effects (model_runs + predictions per theme that trains), but
    does not save top terms -- embedding dimensions aren't interpretable
    vocabulary terms the way TF-IDF coefficients are, so model_top_terms
    simply has no rows for embed_v1_* versions. Checks embedding_readiness()
    up front and returns a descriptive error instead of attempting anything
    if either condition isn't met. scheme_id scopes both that readiness check
    and the themes trained, the same way it does for run_training_pass() --
    otherwise another scheme reaching the trust floor could unlock a pass
    over this one."""
    def emit(event):
        if progress_callback:
            progress_callback(event)

    if negative_mode not in NEGATIVE_MODES:
        raise ValueError(f"unknown negative_mode '{negative_mode}' -- expected one of {NEGATIVE_MODES}")
    schema.init_db()
    readiness = embedding_readiness(scheme_id=scheme_id)
    if not readiness["installed"]:
        return {"model_version": None, "trained_at": None, "themes_total": 0, "results": [],
                "error": "sentence-transformers isn't installed -- run "
                         "`pip install -r requirements-embeddings.txt` first."}
    if not readiness["volume_ready"]:
        return {"model_version": None, "trained_at": None, "themes_total": 0, "results": [],
                "error": f"No theme has reached the {TRUST_POSITIVES}-positive trust floor yet -- "
                         "keep coding, then try again."}

    theme_list = themes.list_themes(scheme_id=scheme_id)
    if not theme_list:
        return {"model_version": None, "trained_at": None, "themes_total": 0,
                "results": [], "error": _no_themes_error(scheme_id)}

    emit({"stage": "corpus"})
    segment_ids, texts, duplicate_run_id, excluded_dataset_ids = build_corpus(
        exclude_duplicates=exclude_duplicates
    )
    if not segment_ids:
        return {"model_version": None, "trained_at": None, "themes_total": len(theme_list),
                "results": [],
                "error": "No segments in coding.db -- run scripts/build_segments.py first."}

    emit({"stage": "embedding", "n_segments": len(segment_ids)})
    X = build_embeddings(segment_ids, texts)

    model_version = "embed_v1_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    trained_at = datetime.now(timezone.utc).isoformat()
    activity_log.save_model_run_params(
        model_version,
        embedding_training_params(scheme_id=scheme_id, negative_mode=negative_mode,
                                  codebook_versions=codebook_export.codebook_versions(scheme_id)),
        trained_at,
    )

    results = []
    for i, theme in enumerate(theme_list):
        emit({"stage": "theme_start", "index": i, "total": len(theme_list),
              "theme_id": theme["theme_id"], "name": theme["name"]})
        row_idx, y, n_pos, n_neg = training_labels(theme["theme_id"], segment_ids, negative_mode)

        skip = _skip_status(n_pos, n_neg, negative_mode, TRUST_POSITIVES)
        if skip:
            results.append({
                "theme_id": theme["theme_id"], "name": theme["name"],
                "status": skip, "n_pos": n_pos, "n_neg": n_neg, "metrics": None,
            })
            emit({"stage": "theme_skipped", "index": i, "total": len(theme_list),
                  "theme_id": theme["theme_id"], "name": theme["name"],
                  "status": skip, "n_pos": n_pos, "n_neg": n_neg})
            continue

        # n_pos >= TRUST_POSITIVES > MIN_POSITIVES_ATTEMPT, so train_theme always succeeds here.
        metrics, model, oof_scores = _fit_on_rows(X, row_idx, y, n_pos)
        model_runs.save_model_run(model_version, theme["theme_id"], trained_at, metrics)

        scores = model.predict_proba(X)[:, 1]
        model_runs.save_predictions(model_version, theme["theme_id"], segment_ids, scores, oof_scores)

        results.append({
            "theme_id": theme["theme_id"], "name": theme["name"],
            "status": "trained", "n_pos": n_pos, "n_neg": n_neg, "metrics": metrics,
        })
        emit({"stage": "theme_done", "index": i, "total": len(theme_list),
              "theme_id": theme["theme_id"], "name": theme["name"], "metrics": metrics})

    return {"model_version": model_version, "trained_at": trained_at,
            "themes_total": len(theme_list), "results": results, "error": None}
