"""The shared foundation every other coding_store submodule sits on: the
SQLite schema itself, the connection, and schema migration. No dependency on
any other coding_store submodule -- segments.py, themes.py, codes.py,
model_runs.py, activity_log.py, duplicates.py, and dataset_status.py all
import this one, never each other's storage primitives.

This is the only persistent database in the app -- Browse/Search & Export
stay flat-JSON and in-memory, per DEVELOPMENT.md. Scoped deliberately to
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

CREATE TABLE IF NOT EXISTS negatives (
    negative_id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id TEXT NOT NULL,
    theme_id TEXT NOT NULL,
    coder TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(segment_id, theme_id, coder)
);

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
-- see coding.js/review.js and DEVELOPMENT.md's Coding subsystem section.
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

CURRENT_SCHEMA_VERSION = 2


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
