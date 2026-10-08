"""The shared foundation every other coding_store submodule sits on: the
SQLite schema itself, the connection, and schema migration. No dependency on
any other coding_store submodule -- segments.py, themes.py, codes.py,
model_runs.py, activity_log.py, duplicates.py, and dataset_status.py all
import this one, never each other's storage primitives.

This is the only persistent database in the app -- Browse/Search & Export
stay flat-JSON and in-memory, per docs/DEVELOPMENT.md. Scoped deliberately to
coding/ML data only, because that's a point-mutation, joinable workload that
flat JSON handles poorly at tens-of-thousands-of-segments scale.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from paths import DB_PATH

DEFAULT_CODER = "researcher"

SQLITE_MAX_VARIABLES = 900  # conservative -- SQLite's actual cap is 999 pre-3.32, 32766 after;
                             # 900 stays safely under either. Chunk any IN (...) query built
                             # from a caller-supplied id list at this size (see
                             # segments.get_segments_by_ids()/codes.get_codes_for_segment_ids())
                             # -- a real corpus routinely has tens of thousands of segments, and
                             # a single unbounded IN (...) blows past the limit with
                             # "too many SQL variables" once it does.

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    segment_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    group_name TEXT,
    person_name TEXT,
    seg_index INTEGER NOT NULL,
    speaker_name TEXT,
    speaker_id INTEGER,
    speaker_role TEXT,
    depth INTEGER,
    timestamp TEXT,
    text TEXT NOT NULL,
    word_count INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_feed_item ON segments(item_id);
CREATE INDEX IF NOT EXISTS idx_segments_dataset ON segments(dataset_id);

CREATE TABLE IF NOT EXISTS schemes (
    scheme_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS themes (
    theme_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    example_words TEXT,
    color TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS codes (
    code_id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id TEXT NOT NULL,
    theme_id TEXT NOT NULL,
    coder TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    note TEXT,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_codes_segment ON codes(segment_id);
CREATE INDEX IF NOT EXISTS idx_codes_theme ON codes(theme_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_codes_active
    ON codes(segment_id, theme_id, coder) WHERE deleted_at IS NULL;

-- Explicit rejections: a coder's decision that a theme does NOT apply to a segment
-- (see codes.add_negative()). Same provenance/soft-delete shape as `codes`
-- (source/note/deleted_at, all added in schema_version 5). Note the table-level
-- UNIQUE also covers soft-deleted rows -- unlike codes' partial unique index -- so
-- re-rejecting reactivates the existing row rather than inserting a new one.
CREATE TABLE IF NOT EXISTS negatives (
    negative_id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id TEXT NOT NULL,
    theme_id TEXT NOT NULL,
    coder TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    note TEXT,
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    UNIQUE(segment_id, theme_id, coder)
);
CREATE INDEX IF NOT EXISTS idx_negatives_segment ON negatives(segment_id);
CREATE INDEX IF NOT EXISTS idx_negatives_theme ON negatives(theme_id);

-- "Reviewed" state (roadmap A6), both scoped to one coding scheme and timestamped.
-- item_reviews: a researcher marked a whole item (interview/thread) "fully coded"
-- for a scheme -- every uncoded, non-interviewer segment in it was considered
-- against that scheme's themes *as they existed at marked_at*. Unmarking sets
-- unmarked_at; re-marking adds a new row, so the history is kept.
-- segment_none_confirmations: the per-segment equivalent (the Code tab's `n` key):
-- "none of this scheme's themes apply here".
-- coding_store/labels.py turns these into explicit negatives for training's
-- `reviewed` negative mode: a segment only counts against a theme created *before*
-- the mark/confirmation, since a later theme was never considered.
CREATE TABLE IF NOT EXISTS item_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    scheme_id TEXT NOT NULL,
    coder TEXT NOT NULL,
    marked_at TEXT NOT NULL,     -- when the mark takes effect (decides which themes it covers)
    unmarked_at TEXT,
    recorded_at TEXT             -- when it was actually entered; differs from marked_at only for
                                 -- a bulk mark dated "as of" an earlier time (schema_version 6)
);
CREATE INDEX IF NOT EXISTS idx_item_reviews_lookup ON item_reviews(scheme_id, dataset_id, item_id);

-- Triage decisions from the Review tab's queues (roadmap B2) -- see
-- coding_store/decisions.py. One row per accept/reject/skip, with the queue context it
-- was made in. Skips are recorded too: roadmap C2's precision estimate from the
-- random-check queue needs every passage shown. Undo stamps undone_at; never deleted.
CREATE TABLE IF NOT EXISTS review_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL,          -- model_version:queue_theme_id:mode
    segment_id TEXT NOT NULL,
    theme_id TEXT NOT NULL,          -- the theme decided (the *other* theme in the overlap queue)
    queue_theme_id TEXT NOT NULL,    -- the queue's own theme
    mode TEXT NOT NULL,
    engine TEXT,
    model_version TEXT,
    score REAL,                      -- the model's score for theme_id when the decision was made
    oof_score REAL,
    decision TEXT NOT NULL,          -- 'accept' | 'reject' | 'skip'
    prev_state TEXT NOT NULL,        -- 'coded' | 'rejected' | 'none', for undo
    flag TEXT,                       -- 'exemplar' (with accept) | 'near_miss' (with reject) | NULL (v8)
    prev_flag INTEGER,               -- the decided row's flag before, for undo (v8)
    prev_flag_note TEXT,
    coder TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    undone_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_decisions_round ON review_decisions(round_id);
CREATE INDEX IF NOT EXISTS idx_review_decisions_segment ON review_decisions(segment_id, theme_id);

CREATE TABLE IF NOT EXISTS segment_none_confirmations (
    confirmation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id TEXT NOT NULL,
    scheme_id TEXT NOT NULL,
    coder TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    unconfirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_segment_none_lookup ON segment_none_confirmations(scheme_id, segment_id);

CREATE TABLE IF NOT EXISTS embeddings (
    segment_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (segment_id, model_name)
);

CREATE TABLE IF NOT EXISTS predictions (
    prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id TEXT NOT NULL,
    theme_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    score REAL NOT NULL,
    oof_score REAL,
    created_at TEXT NOT NULL,
    UNIQUE(segment_id, theme_id, model_version)
);
CREATE INDEX IF NOT EXISTS idx_predictions_theme ON predictions(theme_id, model_version);

CREATE TABLE IF NOT EXISTS model_runs (
    model_version TEXT NOT NULL,
    theme_id TEXT NOT NULL,
    trained_at TEXT NOT NULL,
    n_pos INTEGER,
    n_neg INTEGER,
    precision REAL,
    recall REAL,
    f1 REAL,
    pr_auc REAL,
    threshold REAL,
    PRIMARY KEY (model_version, theme_id)
);

-- Training passes the researcher has pinned (e.g. a run cited in a paper or used for
-- an export): coding_store.storage never prunes a pinned run's per-segment scores.
-- One row per pinned model_version; unpinning deletes the row (the pin itself is
-- logged to activity_log either way). See coding_store/storage.py.
CREATE TABLE IF NOT EXISTS model_run_pins (
    model_version TEXT PRIMARY KEY,
    pinned_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS model_top_terms (
    model_version TEXT NOT NULL,
    theme_id TEXT NOT NULL,
    direction TEXT NOT NULL,   -- 'positive' | 'negative'
    rank INTEGER NOT NULL,     -- 1..N within (model_version, theme_id, direction)
    term TEXT NOT NULL,
    weight REAL NOT NULL,
    PRIMARY KEY (model_version, theme_id, direction, rank)
);
CREATE INDEX IF NOT EXISTS idx_top_terms_theme ON model_top_terms(theme_id, model_version);

CREATE TABLE IF NOT EXISTS cluster_runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    algorithm TEXT,
    model_name TEXT,
    n_clusters INTEGER
);

CREATE TABLE IF NOT EXISTS clusters (
    run_id TEXT NOT NULL,
    cluster_id INTEGER NOT NULL,
    size INTEGER,
    top_terms TEXT,
    exemplar_segment_ids TEXT,
    PRIMARY KEY (run_id, cluster_id)
);

CREATE TABLE IF NOT EXISTS cluster_segments (
    run_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    cluster_id INTEGER NOT NULL,
    PRIMARY KEY (run_id, segment_id)
);

CREATE TABLE IF NOT EXISTS consistency_checks (
    check_id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id TEXT NOT NULL,
    theme_id TEXT NOT NULL,
    original_code_id INTEGER,
    recheck_decision TEXT,
    agree INTEGER,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Cross-cutting research-action log for the Web Appendix tab: covers actions
-- that existing tables can't attribute an actor to at all (theme CRUD,
-- training runs, downloads, filter snapshots). Deliberately separate from
-- codes.coder/source/note, which already covers per-code provenance well --
-- see coding.js/review.js and docs/DEVELOPMENT.md's Coding subsystem section.
CREATE TABLE IF NOT EXISTS activity_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action_type TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_log_ts ON activity_log(ts);
CREATE INDEX IF NOT EXISTS idx_activity_log_action ON activity_log(action_type);

-- One row per training pass (not per theme): the shared hyperparameters used,
-- since model_runs' per-theme metrics say nothing about how they were
-- produced. See classifier.py's run_training_pass().
CREATE TABLE IF NOT EXISTS model_run_params (
    model_version TEXT PRIMARY KEY,
    params_json TEXT NOT NULL,
    trained_at TEXT NOT NULL
);

-- Near-duplicate detection (retroactive audit over queries/*.json items -- e.g. the
-- same keynote independently re-transcribed by many outlets). Mark-only: nothing here
-- ever deletes/hides a queries/*.json record or touches `codes`. Keyed by
-- (dataset_id, item_id) -- item_id is unique only within a dataset -- not segment_id,
-- so this can run independently of scripts/build_segments.py. See
-- scripts/find_duplicates.py and classifier.build_corpus()'s consultation of
-- duplicate_cluster_items.
CREATE TABLE IF NOT EXISTS duplicate_runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    method TEXT NOT NULL,           -- e.g. 'tfidf_cosine_v1'
    params_json TEXT NOT NULL,      -- window_days, threshold, ngram_range, etc.
    n_items_scanned INTEGER,
    n_clusters INTEGER
);

CREATE TABLE IF NOT EXISTS duplicate_clusters (
    run_id TEXT NOT NULL,
    cluster_id INTEGER NOT NULL,
    person_name TEXT,
    size INTEGER NOT NULL,
    min_pairwise_similarity REAL,
    mean_pairwise_similarity REAL,
    canonical_dataset_id TEXT,
    canonical_item_id TEXT,
    canonical_reason TEXT,           -- 'has_active_codes' | 'longest_text' |
                                      -- 'highest_view_count' | 'tiebreak_id'
    needs_attention TEXT,            -- NULL | 'low_cohesion' | 'canonical_conflict'
                                      -- | 'coded_non_canonical'
    PRIMARY KEY (run_id, cluster_id)
);
CREATE INDEX IF NOT EXISTS idx_dup_clusters_attention
    ON duplicate_clusters(run_id, needs_attention);

CREATE TABLE IF NOT EXISTS duplicate_cluster_items (
    run_id TEXT NOT NULL,
    cluster_id INTEGER NOT NULL,
    dataset_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    is_canonical INTEGER NOT NULL DEFAULT 0,
    similarity_to_canonical REAL,
    containment_in_canonical REAL,
    word_count INTEGER,
    view_count INTEGER,
    source_name TEXT,
    publish_date TEXT,
    PRIMARY KEY (run_id, dataset_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_dup_cluster_items_lookup
    ON duplicate_cluster_items(dataset_id, item_id);
CREATE INDEX IF NOT EXISTS idx_dup_cluster_items_run_cluster
    ON duplicate_cluster_items(run_id, cluster_id);

-- Researcher corrections to scripts/find_duplicates.py's automated output --
-- deliberately NOT tied to any one duplicate_runs row, so a correction survives a
-- future re-run (which recomputes duplicate_clusters/duplicate_cluster_items from
-- scratch). 'exclude': this item is NOT a duplicate, overriding whatever the latest
-- run says (a false positive). 'include': this item IS a duplicate of
-- (canonical_dataset_id, canonical_item_id), regardless of what -- or whether -- any
-- run says (a false negative, or a pair the detector never even compared). One row
-- per (dataset_id, item_id); a later call replaces the prior override for that item.
-- Consulted by classifier.build_corpus() (always wins over the automated run) and by
-- coding_store.duplicates.get_duplicate_status_for_dataset() (the Browse/Coding
-- "duplicate of ..." flag).
CREATE TABLE IF NOT EXISTS duplicate_overrides (
    dataset_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    action TEXT NOT NULL,              -- 'exclude' | 'include'
    canonical_dataset_id TEXT,         -- required for 'include', NULL for 'exclude'
    canonical_item_id TEXT,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (dataset_id, item_id)
);

-- Whole-dataset ("corpus") inclusion state, set from the Coding tab's Datasets panel.
-- Absence of a row means the dataset is active/included, same "mark-only" idiom as
-- duplicate_overrides above. 'excluded': stays fully visible/codeable in Browse and
-- Coding, just left out of classifier.build_corpus()'s training corpus. 'unloaded':
-- hidden everywhere (viewer_server.list_datasets()/get_dataset()) as if it didn't
-- exist, without touching its segments/codes/source file -- restorable from the same
-- panel. Never deletes anything; see classifier.build_corpus()'s consultation of this
-- table and viewer_server.py's dataset endpoints.
CREATE TABLE IF NOT EXISTS dataset_status (
    dataset_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,      -- 'excluded' | 'unloaded'
    actor TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    note TEXT
);
"""

CURRENT_SCHEMA_VERSION = 8


def _now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _stamp_schema_version(conn)


def set_db_path(path):
    """Repoint this module's storage at a different coding.db, and initialize it
    (creates a fresh schema for a new project, migrates an existing one) -- used
    by viewer_server.py's in-app project switcher (see project_registry.py) to
    live-switch the active project without restarting the server. Works because
    get_conn() looks up the module-level DB_PATH name at call time, not a
    captured value, so reassigning it here is sufficient."""
    global DB_PATH
    DB_PATH = Path(path)
    init_db()


def _migrate(conn):
    """Additive, idempotent schema tweaks that CREATE TABLE IF NOT EXISTS can't
    express (new columns on an existing table, or renaming one)."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(themes)")}
    if "merged_into" not in cols:
        conn.execute("ALTER TABLE themes ADD COLUMN merged_into TEXT")

    # schema_version 2: generalize the segments table's field names beyond the
    # "executive interview" domain (company/executive/feed_item_id -> group_name/
    # person_name/item_id), and add speaker_role/depth for source-specific
    # semantics (interviewer-vs-subject exclusion, Reddit-style reply nesting).
    # ALTER TABLE ... RENAME COLUMN only rewrites the schema catalog -- row data
    # is stored positionally, never keyed by name, so this is data-preserving.
    seg_cols = {row["name"] for row in conn.execute("PRAGMA table_info(segments)")}
    if "company" in seg_cols:
        conn.execute("ALTER TABLE segments RENAME COLUMN company TO group_name")
    if "executive" in seg_cols:
        conn.execute("ALTER TABLE segments RENAME COLUMN executive TO person_name")
    if "feed_item_id" in seg_cols:
        conn.execute("ALTER TABLE segments RENAME COLUMN feed_item_id TO item_id")

    seg_cols = {row["name"] for row in conn.execute("PRAGMA table_info(segments)")}
    if "speaker_role" not in seg_cols:
        conn.execute("ALTER TABLE segments ADD COLUMN speaker_role TEXT")
    if "depth" not in seg_cols:
        conn.execute("ALTER TABLE segments ADD COLUMN depth INTEGER")

    # schema_version 3: multiple independent coding schemes + hierarchical
    # (parent/child) themes within a scheme. theme_id itself is NOT rewritten
    # for pre-existing themes -- only themes created after this migration get
    # the scheme-prefixed theme_id shape (see themes.create_theme()) -- so no
    # codes/predictions/model_runs/model_top_terms row anywhere needs to
    # change at all. See docs/DEVELOPMENT.md's Coding subsystem section.
    theme_cols = {row["name"] for row in conn.execute("PRAGMA table_info(themes)")}
    if "scheme_id" not in theme_cols:
        conn.execute("ALTER TABLE themes ADD COLUMN scheme_id TEXT")
    if "parent_theme_id" not in theme_cols:
        conn.execute("ALTER TABLE themes ADD COLUMN parent_theme_id TEXT")
    if "depth" not in theme_cols:
        conn.execute("ALTER TABLE themes ADD COLUMN depth INTEGER NOT NULL DEFAULT 0")
    if "sort_order" not in theme_cols:
        conn.execute("ALTER TABLE themes ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")

    # Backfill: every pre-v3 theme (scheme_id IS NULL) is assigned to one
    # default scheme created here, so an existing coding.db upgrades with
    # zero data loss and zero manual action.
    orphaned = conn.execute(
        "SELECT COUNT(*) AS n FROM themes WHERE scheme_id IS NULL"
    ).fetchone()["n"]
    if orphaned:
        now = _now()
        conn.execute(
            """INSERT OR IGNORE INTO schemes (scheme_id, name, description, status, created_at, updated_at)
               VALUES ('default', 'Default scheme',
                       'Auto-created during the v3 migration to hold every theme that existed before multi-scheme support.',
                       'active', ?, ?)""",
            (now, now),
        )
        conn.execute("UPDATE themes SET scheme_id='default' WHERE scheme_id IS NULL")

    # schema_version 4: predictions.oof_score -- the cross-validated, out-of-fold
    # score from a model that never saw that segment (see classifier.train_theme()).
    # `score` comes from the final model fit on every label, so a coded segment's
    # `score` is inflated by construction; oof_score is the honest one, needed for
    # spotting likely mislabels. NULL for every pre-v4 row and for segments a run
    # didn't train on -- both read as "no out-of-fold score", never as 0.
    pred_cols = {row["name"] for row in conn.execute("PRAGMA table_info(predictions)")}
    if "oof_score" not in pred_cols:
        conn.execute("ALTER TABLE predictions ADD COLUMN oof_score REAL")

    # schema_version 5: explicit rejections become real research data (see
    # codes.add_negative()), so they get the same provenance and soft-delete
    # columns as `codes` -- rule 5: decisions are never hard-deleted. Every
    # pre-v5 row (there were none in practice: nothing wrote negatives before
    # v5) reads as an active, 'manual' rejection.
    neg_cols = {row["name"] for row in conn.execute("PRAGMA table_info(negatives)")}
    if "source" not in neg_cols:
        conn.execute("ALTER TABLE negatives ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
    if "note" not in neg_cols:
        conn.execute("ALTER TABLE negatives ADD COLUMN note TEXT")
    if "deleted_at" not in neg_cols:
        conn.execute("ALTER TABLE negatives ADD COLUMN deleted_at TEXT")

    # schema_version 6: item_reviews.recorded_at -- a bulk "fully coded" mark can be
    # dated as of an earlier time (e.g. when each interview was last coded, see
    # reviewed.bulk_mark_items()); recorded_at keeps when it was actually entered, so
    # the record never claims a mark existed earlier than it did. NULL on older rows,
    # where recorded_at == marked_at.
    review_cols = {row["name"] for row in conn.execute("PRAGMA table_info(item_reviews)")}
    if "recorded_at" not in review_cols:
        conn.execute("ALTER TABLE item_reviews ADD COLUMN recorded_at TEXT")

    # schema_version 7: codebook rigor (docs/CODEBOOK_PLAN.md K1) -- what an external or
    # embedding model needs beyond a one-line description. Criteria and boundary notes on
    # themes; exemplar / near-miss flags (with their own notes, separate from the
    # provenance text codes.note already carries) on the code / rejection row itself, so
    # a flag can only exist on an active row and goes away with it if that row is
    # soft-deleted; and a separate "read in full" confirmation on item_reviews that never
    # touches marked_at (which decides which themes a mark covers, see labels.py).
    theme_cols = {row["name"] for row in conn.execute("PRAGMA table_info(themes)")}
    for col in ("inclusion_criteria", "exclusion_criteria", "boundary_notes"):
        if col not in theme_cols:
            conn.execute(f"ALTER TABLE themes ADD COLUMN {col} TEXT")
    code_cols = {row["name"] for row in conn.execute("PRAGMA table_info(codes)")}
    if "is_exemplar" not in code_cols:
        conn.execute("ALTER TABLE codes ADD COLUMN is_exemplar INTEGER NOT NULL DEFAULT 0")
    if "exemplar_note" not in code_cols:
        conn.execute("ALTER TABLE codes ADD COLUMN exemplar_note TEXT")
    neg_cols = {row["name"] for row in conn.execute("PRAGMA table_info(negatives)")}
    if "is_near_miss" not in neg_cols:
        conn.execute("ALTER TABLE negatives ADD COLUMN is_near_miss INTEGER NOT NULL DEFAULT 0")
    if "near_miss_note" not in neg_cols:
        conn.execute("ALTER TABLE negatives ADD COLUMN near_miss_note TEXT")
    review_cols = {row["name"] for row in conn.execute("PRAGMA table_info(item_reviews)")}
    if "read_in_full_confirmed_at" not in review_cols:
        conn.execute("ALTER TABLE item_reviews ADD COLUMN read_in_full_confirmed_at TEXT")

    # schema_version 8: Review triage can flag while deciding -- `e` accepts and flags an
    # exemplar, `m` rejects and flags a near miss (CODEBOOK_PLAN K3). The decision stays
    # accept/reject, so per-round accept counts are unaffected; `flag` records the flag,
    # and prev_flag/prev_flag_note what the row had before, so undo can restore it.
    decision_cols = {row["name"] for row in conn.execute("PRAGMA table_info(review_decisions)")}
    if "flag" not in decision_cols:
        conn.execute("ALTER TABLE review_decisions ADD COLUMN flag TEXT")
    if "prev_flag" not in decision_cols:
        conn.execute("ALTER TABLE review_decisions ADD COLUMN prev_flag INTEGER")
    if "prev_flag_note" not in decision_cols:
        conn.execute("ALTER TABLE review_decisions ADD COLUMN prev_flag_note TEXT")


def _stamp_schema_version(conn):
    """Lightweight version tracking: one row in schema_meta, bumped by hand in
    _migrate() whenever a future change needs a real migration step (not just
    an additive CREATE TABLE IF NOT EXISTS, which needs no version bump at
    all). No migration framework -- same ad hoc idiom as the merged_into
    check above, just keyed off a counter instead of a column-existence
    probe. A DB stamped ahead of the running code's version (e.g. a
    researcher rolled back code but kept a newer coding.db) gets a warning,
    not a crash -- this is a solo-researcher tool, not a service to protect."""
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (str(CURRENT_SCHEMA_VERSION),),
        )
        return
    stored_version = int(row["value"])
    if stored_version > CURRENT_SCHEMA_VERSION:
        print(f"Warning: coding.db is stamped schema_version={stored_version}, but this code "
              f"only knows schema_version={CURRENT_SCHEMA_VERSION}. Update the code, or point "
              f"it at an older coding.db, before relying on it.")
    elif stored_version < CURRENT_SCHEMA_VERSION:
        conn.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            (str(CURRENT_SCHEMA_VERSION),),
        )
