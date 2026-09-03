"""SQLite storage for the coding subsystem: segments, the researcher's
codebook (themes), codes applied to segments, and (in later phases)
embeddings metadata, classifier predictions, and topic-discovery output.

This is the only persistent database in the app -- Browse/Search & Export
stay flat-JSON and in-memory, per DEVELOPMENT.md. Scoped deliberately to
coding/ML data only, because that's a point-mutation, joinable workload that
flat JSON handles poorly at tens-of-thousands-of-segments scale.
"""
import csv
import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from paths import DB_PATH

DEFAULT_CODER = "researcher"

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
-- coding_store.get_duplicate_status_for_dataset() (the Browse/Coding "duplicate of
-- ..." flag).
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


# --- segments -----------------------------------------------------------------------

def upsert_segments(segments):
    """Insert or replace a batch of segment dicts (see segmentation.py for shape).
    Idempotent -- safe to re-run scripts/build_segments.py."""
    now = _now()
    rows = [
        (
            s["segment_id"], str(s["item_id"]), s["dataset_id"], s.get("group_name"),
            s.get("person_name"), s["seg_index"], s.get("speaker_name"), s.get("speaker_id"),
            s.get("speaker_role"), s.get("depth"), s.get("timestamp"), s["text"],
            s.get("word_count"), now,
        )
        for s in segments
    ]
    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO segments
                 (segment_id, item_id, dataset_id, group_name, person_name, seg_index,
                  speaker_name, speaker_id, speaker_role, depth, timestamp, text,
                  word_count, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(segment_id) DO UPDATE SET
                 item_id=excluded.item_id, dataset_id=excluded.dataset_id,
                 group_name=excluded.group_name, person_name=excluded.person_name,
                 seg_index=excluded.seg_index, speaker_name=excluded.speaker_name,
                 speaker_id=excluded.speaker_id, speaker_role=excluded.speaker_role,
                 depth=excluded.depth, timestamp=excluded.timestamp,
                 text=excluded.text, word_count=excluded.word_count""",
            rows,
        )


def count_segments(dataset_id=None):
    with get_conn() as conn:
        if dataset_id:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM segments WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM segments").fetchone()
    return row["n"]


def get_segments_for_item(item_id):
    """item_id is always compared/returned as a string -- some existing rows may still be
    stored with SQLite's INTEGER storage class (the column kept its original affinity
    across the schema_version 2 rename; SQLite's affinity-aware comparison still matches
    a string parameter against those rows correctly), but the dict this returns always
    normalizes item_id to str so callers (JSON responses, JS) see one consistent type."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM segments WHERE item_id=? ORDER BY seg_index",
            (str(item_id),),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["item_id"] = str(d["item_id"])
        out.append(d)
    return out


# --- themes (the researcher's codebook) ----------------------------------------------

def _slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return text or "theme"


def list_themes(include_archived=False):
    with get_conn() as conn:
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


def create_theme(name, description="", example_words="", color="#6b7fd7", actor=DEFAULT_CODER):
    name = name.strip()
    if not name:
        raise ValueError("theme name is required")
    base_id = _slugify(name)
    now = _now()
    with get_conn() as conn:
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
    log_activity(actor, "theme_create", {"theme_id": theme_id, "name": name, "description": description})
    return get_theme(theme_id)


def get_theme(theme_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM themes WHERE theme_id=?", (theme_id,)
        ).fetchone()
    return dict(row) if row else None


def update_theme(theme_id, fields, actor=DEFAULT_CODER):
    allowed = {"name", "description", "example_words", "color"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_theme(theme_id)
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE themes SET {set_clause} WHERE theme_id=?",
            (*updates.values(), theme_id),
        )
    log_activity(actor, "theme_update", {"theme_id": theme_id,
                                          "fields": {k: v for k, v in updates.items() if k != "updated_at"}})
    return get_theme(theme_id)


def set_theme_status(theme_id, status, actor=DEFAULT_CODER):
    with get_conn() as conn:
        conn.execute(
            "UPDATE themes SET status=?, updated_at=? WHERE theme_id=?",
            (status, _now(), theme_id),
        )
    log_activity(actor, "theme_archive" if status == "archived" else "theme_restore",
                 {"theme_id": theme_id, "status": status})
    return get_theme(theme_id)


def merge_themes(source_theme_id, target_theme_id, actor=DEFAULT_CODER):
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

    now = _now()
    with get_conn() as conn:
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
    log_activity(actor, "theme_merge", {
        "source_theme_id": source_theme_id, "target_theme_id": target_theme_id,
    })
    return get_theme(target_theme_id)


# --- codes ----------------------------------------------------------------------------

def get_codes_for_segment_ids(segment_ids):
    """Returns {segment_id: [theme_id, ...]} for active codes on the given segments."""
    if not segment_ids:
        return {}
    placeholders = ",".join("?" * len(segment_ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT segment_id, theme_id FROM codes
                WHERE deleted_at IS NULL AND segment_id IN ({placeholders})""",
            segment_ids,
        ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["segment_id"], []).append(r["theme_id"])
    return out


def add_code(segment_id, theme_id, coder=DEFAULT_CODER, source="manual", note=None):
    now = _now()
    with get_conn() as conn:
        existing = conn.execute(
            """SELECT code_id, deleted_at FROM codes
               WHERE segment_id=? AND theme_id=? AND coder=?
               ORDER BY code_id DESC LIMIT 1""",
            (segment_id, theme_id, coder),
        ).fetchone()
        if existing and existing["deleted_at"] is None:
            return  # already active, no-op
        if existing:
            conn.execute(
                "UPDATE codes SET deleted_at=NULL, source=?, note=?, created_at=? WHERE code_id=?",
                (source, note, now, existing["code_id"]),
            )
        else:
            conn.execute(
                """INSERT INTO codes (segment_id, theme_id, coder, source, note, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (segment_id, theme_id, coder, source, note, now),
            )


def remove_code(segment_id, theme_id, coder=DEFAULT_CODER):
    with get_conn() as conn:
        conn.execute(
            """UPDATE codes SET deleted_at=? WHERE segment_id=? AND theme_id=? AND coder=?
               AND deleted_at IS NULL""",
            (_now(), segment_id, theme_id, coder),
        )


def add_negative(segment_id, theme_id, coder=DEFAULT_CODER):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO negatives (segment_id, theme_id, coder, created_at)
               VALUES (?,?,?,?)""",
            (segment_id, theme_id, coder, _now()),
        )


# --- progress ---------------------------------------------------------------------------

# --- classifier (model_runs, predictions) ------------------------------------------

def save_model_run(model_version, theme_id, trained_at, metrics):
    with get_conn() as conn:
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


def save_predictions(model_version, theme_id, segment_ids, scores):
    now = _now()
    rows = [(sid, theme_id, model_version, float(score), now) for sid, score in zip(segment_ids, scores)]
    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO predictions (segment_id, theme_id, model_version, score, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(segment_id, theme_id, model_version) DO UPDATE SET
                 score=excluded.score, created_at=excluded.created_at""",
            rows,
        )


def list_model_runs(theme_id=None):
    with get_conn() as conn:
        if theme_id:
            rows = conn.execute(
                "SELECT * FROM model_runs WHERE theme_id=? ORDER BY trained_at DESC", (theme_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM model_runs ORDER BY trained_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_latest_model_runs():
    """{theme_id: latest model_run dict}, one row per theme (its most recent run)."""
    with get_conn() as conn:
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
    with get_conn() as conn:
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
    with get_conn() as conn:
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
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT p.segment_id, p.score, s.item_id, s.dataset_id, s.group_name,
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


def get_value_prop(theme_id, model_version, threshold):
    """coded_count (from codes) vs. flagged_uncoded_count (segments the model
    scores above threshold for this run that aren't already coded) -- the
    headline "the model found N more cases" stat."""
    with get_conn() as conn:
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
    with get_conn() as conn:
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
        seg_codes = get_codes_for_segment_ids(coded_segment_ids)
        for sid, tids in seg_codes.items():
            for tid in tids:
                if tid in theme_ids:
                    candidates[sid]["reasons"].append({"type": "coded", "theme_id": tid})

    latest_runs = get_latest_model_runs()
    for theme_id in theme_ids:
        run = latest_runs.get(theme_id)
        if not run:
            continue
        for p in get_top_predictions(theme_id, run["model_version"], limit=predicted_limit):
            if p["already_coded"]:
                continue
            c = ensure_candidate(p)
            c["reasons"].append({"type": "predicted", "theme_id": theme_id, "score": p["score"]})

    all_ids = list(candidates.keys())
    codes_by_segment = get_codes_for_segment_ids(all_ids)
    for sid, c in candidates.items():
        c["theme_ids"] = codes_by_segment.get(sid, [])

    return list(candidates.values())


def get_progress(dataset_id=None):
    with get_conn() as conn:
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


# --- codebook export (backup / publication supplementary material) ------------------

THEME_EXPORT_FIELDS = [
    "theme_id", "name", "description", "example_words", "color", "status",
    "merged_into", "created_at", "updated_at",
]
CODE_EXPORT_FIELDS = [
    "code_id", "segment_id", "theme_id", "coder", "source", "note", "created_at", "deleted_at",
]
MODEL_RUN_EXPORT_FIELDS = [
    "model_version", "theme_id", "trained_at", "n_pos", "n_neg",
    "precision", "recall", "f1", "pr_auc", "threshold",
]


def export_codebook(output_dir):
    """Dump the researcher's actual codebook -- every theme regardless of status,
    every code including soft-deleted ones (deleted_at is provenance, not
    deletion), and the latest model_runs -- to JSON+CSV pairs in output_dir.
    This is the only export of coding.db's substance the app offers: use it as
    a backup, or as a journal's supplementary material. Mirrors
    query_api.write_export's JSON+CSV shape so both export paths in the app
    produce the same kind of file on disk. Returns the list of paths written.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(exist_ok=True)

    with get_conn() as conn:
        themes = [dict(r) for r in conn.execute("SELECT * FROM themes ORDER BY name").fetchall()]
        codes = [dict(r) for r in conn.execute("SELECT * FROM codes ORDER BY code_id").fetchall()]
        model_runs = [
            dict(r) for r in
            conn.execute("SELECT * FROM model_runs ORDER BY theme_id, trained_at DESC").fetchall()
        ]

    paths = []
    for name, rows, fields in (
        ("themes", themes, THEME_EXPORT_FIELDS),
        ("codes", codes, CODE_EXPORT_FIELDS),
        ("model_runs", model_runs, MODEL_RUN_EXPORT_FIELDS),
    ):
        json_path = output_dir / f"{name}.json"
        csv_path = output_dir / f"{name}.csv"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        paths.extend([json_path, csv_path])
    return paths


# --- activity log (Web Appendix tab) -------------------------------------------------

def log_activity(actor, action_type, details=None):
    """Append one row to the cross-cutting research-action log. `details` is any
    JSON-serializable dict describing what happened; shape depends on action_type
    (see call sites in viewer_server.py/classifier.py). Never raises on a bad
    `details` value other than a real serialization error -- this is telemetry
    for the Web Appendix tab, not something that should block the action it's
    logging if it fails."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO activity_log (ts, actor, action_type, details_json) VALUES (?,?,?,?)",
            (_now(), actor, action_type, json.dumps(details or {})),
        )


def list_activity(action_type=None, since=None):
    """Activity log rows, newest first, each with `details` parsed back into a
    dict. Optionally filtered to one action_type and/or rows at-or-after `since`
    (an ISO8601 timestamp string, compared lexically like every other
    created_at/ts field in this module)."""
    where = []
    params = []
    if action_type:
        where.append("action_type=?")
        params.append(action_type)
    if since:
        where.append("ts>=?")
        params.append(since)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM activity_log {clause} ORDER BY ts DESC", params
        ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        try:
            row["details"] = json.loads(row.pop("details_json"))
        except (json.JSONDecodeError, TypeError):
            row["details"] = {}
        out.append(row)
    return out


# --- model run parameters (Web Appendix tab) ------------------------------------------

def save_model_run_params(model_version, params, trained_at):
    """One row per training pass: the shared hyperparameters used, since
    model_runs' per-theme metrics say nothing about how they were produced."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO model_run_params (model_version, params_json, trained_at)
               VALUES (?,?,?)
               ON CONFLICT(model_version) DO UPDATE SET
                 params_json=excluded.params_json, trained_at=excluded.trained_at""",
            (model_version, json.dumps(params), trained_at),
        )


def get_appendix_feed(action_type=None, since=None):
    """list_activity()'s rows, enriched for display: theme names resolved from
    any theme_id/source_theme_id/target_theme_id in `details` (looked up
    against every theme regardless of status, since an archived or
    merged-away theme is still part of the historical record), and, for
    training_run rows, the hyperparameters used (joined from
    model_run_params via `model_version` in `details`). This is the
    assembled feed the Web Appendix tab renders."""
    rows = list_activity(action_type=action_type, since=since)
    with get_conn() as conn:
        theme_names = {r["theme_id"]: r["name"] for r in conn.execute("SELECT theme_id, name FROM themes")}
    for row in rows:
        details = row["details"]
        for key in ("theme_id", "source_theme_id", "target_theme_id"):
            tid = details.get(key)
            if tid and theme_names.get(tid):
                details[f"{key}_name"] = theme_names[tid]
        if row["action_type"] == "training_run":
            model_version = details.get("model_version")
            params = get_model_run_params(model_version) if model_version else None
            if params:
                details["params"] = params["params"]
    return rows


def get_model_run_params(model_version):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT params_json, trained_at FROM model_run_params WHERE model_version=?",
            (model_version,),
        ).fetchone()
    if row is None:
        return None
    return {"params": json.loads(row["params_json"]), "trained_at": row["trained_at"]}


# --- near-duplicate detection (scripts/find_duplicates.py) --------------------------

def save_duplicate_run(run_id, created_at, method, params, clusters):
    """Persists one full detection run: the run's own metadata, plus every
    cluster and its members. `clusters` is a list of dicts shaped like:
      {"cluster_id": int, "person_name": str, "size": int,
       "min_pairwise_similarity": float, "mean_pairwise_similarity": float,
       "canonical_dataset_id": str, "canonical_item_id": str,
       "canonical_reason": str, "needs_attention": str|None,
       "members": [{"dataset_id", "item_id", "is_canonical",
                     "similarity_to_canonical", "containment_in_canonical",
                     "word_count", "view_count", "source_name", "publish_date"}, ...]}
    See scripts/find_duplicates.py for how these are built. Every run is kept
    (append-only, same precedent as model_runs) -- classifier.py and callers
    always consult get_latest_duplicate_run_id() for "the current answer"."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO duplicate_runs
                 (run_id, created_at, method, params_json, n_items_scanned, n_clusters)
               VALUES (?,?,?,?,?,?)""",
            (run_id, created_at, method, json.dumps(params), params.get("n_items_scanned"), len(clusters)),
        )
        for c in clusters:
            conn.execute(
                """INSERT INTO duplicate_clusters
                     (run_id, cluster_id, person_name, size, min_pairwise_similarity,
                      mean_pairwise_similarity, canonical_dataset_id, canonical_item_id,
                      canonical_reason, needs_attention)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (run_id, c["cluster_id"], c.get("person_name"), c["size"],
                 c.get("min_pairwise_similarity"), c.get("mean_pairwise_similarity"),
                 c.get("canonical_dataset_id"), c.get("canonical_item_id"),
                 c.get("canonical_reason"), c.get("needs_attention")),
            )
            for m in c["members"]:
                conn.execute(
                    """INSERT INTO duplicate_cluster_items
                         (run_id, cluster_id, dataset_id, item_id, is_canonical,
                          similarity_to_canonical, containment_in_canonical,
                          word_count, view_count, source_name, publish_date)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, c["cluster_id"], m["dataset_id"], m["item_id"],
                     1 if m.get("is_canonical") else 0, m.get("similarity_to_canonical"),
                     m.get("containment_in_canonical"), m.get("word_count"),
                     m.get("view_count"), m.get("source_name"), m.get("publish_date")),
                )


def get_latest_duplicate_run_id():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT run_id FROM duplicate_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return row["run_id"] if row else None


def get_active_code_counts(dataset_item_pairs):
    """{(dataset_id, item_id): active_code_count} for arbitrary (dataset_id,
    item_id) pairs -- unlike get_duplicate_coded_status, doesn't require a
    saved duplicate_runs row to join against, since scripts/find_duplicates.py
    needs this to pick a canonical member *while building* a run, before it's
    saved. Same None-vs-0 semantics: None means not yet segmented."""
    if not dataset_item_pairs:
        return {}
    result = {}
    with get_conn() as conn:
        for dataset_id, item_id in dataset_item_pairs:
            row = conn.execute(
                """SELECT COUNT(s.segment_id) AS n_segments,
                          COUNT(DISTINCT CASE WHEN c.deleted_at IS NULL THEN c.code_id END) AS n_codes
                   FROM segments s LEFT JOIN codes c ON c.segment_id = s.segment_id
                   WHERE s.dataset_id=? AND s.item_id=?""",
                (dataset_id, item_id),
            ).fetchone()
            result[(dataset_id, item_id)] = row["n_codes"] if row["n_segments"] else None
    return result


def list_duplicate_clusters(run_id, needs_attention_only=False):
    """Every cluster in run_id with its member rows attached -- used by
    scripts/find_duplicates.py's printed report, and available for any
    future review UI without re-deriving anything from queries/*.json."""
    with get_conn() as conn:
        clause = "WHERE run_id=?" + (" AND needs_attention IS NOT NULL" if needs_attention_only else "")
        clusters = [
            dict(r) for r in conn.execute(
                f"SELECT * FROM duplicate_clusters {clause} ORDER BY cluster_id", (run_id,)
            ).fetchall()
        ]
        items_by_cluster = {}
        for r in conn.execute(
            "SELECT * FROM duplicate_cluster_items WHERE run_id=? ORDER BY cluster_id, dataset_id, item_id",
            (run_id,),
        ).fetchall():
            items_by_cluster.setdefault(r["cluster_id"], []).append(dict(r))
    for c in clusters:
        c["members"] = items_by_cluster.get(c["cluster_id"], [])
    return clusters


def get_duplicate_coded_status(run_id):
    """{(dataset_id, item_id): active_code_count} for every non-canonical
    duplicate item in run_id, computed live against segments+codes -- never
    trust a stale snapshot for something as consequential as "does this
    already have researcher-applied codes", since that can change any time
    after a detection run. Value is None (not 0) if the item hasn't been
    segmented yet (scripts/build_segments.py not run for it) -- distinct
    from a real 0 (segmented, zero active codes)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT d.dataset_id, d.item_id,
                      COUNT(s.segment_id) AS n_segments,
                      COUNT(DISTINCT CASE WHEN c.deleted_at IS NULL THEN c.code_id END) AS n_codes
               FROM duplicate_cluster_items d
               LEFT JOIN segments s ON s.dataset_id = d.dataset_id AND s.item_id = d.item_id
               LEFT JOIN codes c ON c.segment_id = s.segment_id
               WHERE d.run_id = ? AND d.is_canonical = 0
               GROUP BY d.dataset_id, d.item_id""",
            (run_id,),
        ).fetchall()
    return {
        (r["dataset_id"], r["item_id"]): (r["n_codes"] if r["n_segments"] else None)
        for r in rows
    }


def _count_duplicates_of(conn, dataset_id, item_id, run_id):
    """How many other items are currently duplicates of (dataset_id, item_id)
    after applying overrides -- the automated run's non-canonical members of
    that item's cluster (minus any individually excluded), plus any manual
    'include' overrides pointing at it from anywhere in the corpus. Used for
    the "canonical of N duplicates" badge, which must stay accurate after
    overrides without needing a full corpus recompute."""
    auto_count = 0
    if run_id:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM duplicate_cluster_items di
               JOIN duplicate_clusters dc ON dc.run_id = di.run_id AND dc.cluster_id = di.cluster_id
               WHERE dc.run_id = ? AND dc.canonical_dataset_id = ? AND dc.canonical_item_id = ?
                 AND di.is_canonical = 0
                 AND NOT EXISTS (
                     SELECT 1 FROM duplicate_overrides o
                     WHERE o.dataset_id = di.dataset_id AND o.item_id = di.item_id AND o.action = 'exclude'
                 )""",
            (run_id, dataset_id, item_id),
        ).fetchone()
        auto_count = row["n"]
    manual_row = conn.execute(
        """SELECT COUNT(*) AS n FROM duplicate_overrides
           WHERE action = 'include' AND canonical_dataset_id = ? AND canonical_item_id = ?""",
        (dataset_id, item_id),
    ).fetchone()
    return auto_count + manual_row["n"]


def get_duplicate_status_for_dataset(dataset_id, run_id=None):
    """Effective duplicate status for every item in dataset_id: the latest
    scripts/find_duplicates.py run's cluster membership, with any researcher
    duplicate_overrides layered on top (overrides always win). Used by the
    Browse/Coding tabs' "duplicate of ..." flag -- see
    viewer_server.py's /api/duplicates/status and viewer_static/app.js's
    duplicateSectionHtml()/duplicateListFlagHtml().

    {item_id: {kind: "duplicate", is_canonical, canonical_dataset_id,
                canonical_item_id, size (only when is_canonical), needs_attention,
                source: "auto"|"manual"}
              | {kind: "excluded"}}   -- researcher said "not a duplicate" for an
                                          item the automated run had flagged
    An item absent from the result was never flagged and has no override --
    plain "mark as duplicate" territory in the UI."""
    run_id = run_id or get_latest_duplicate_run_id()
    status = {}
    with get_conn() as conn:
        if run_id:
            rows = conn.execute(
                """SELECT di.item_id, di.is_canonical, dc.canonical_dataset_id,
                          dc.canonical_item_id, dc.needs_attention
                   FROM duplicate_cluster_items di
                   JOIN duplicate_clusters dc ON dc.run_id = di.run_id AND dc.cluster_id = di.cluster_id
                   WHERE di.run_id = ? AND di.dataset_id = ?""",
                (run_id, dataset_id),
            ).fetchall()
            for r in rows:
                status[r["item_id"]] = {
                    "kind": "duplicate", "is_canonical": bool(r["is_canonical"]),
                    "canonical_dataset_id": r["canonical_dataset_id"],
                    "canonical_item_id": r["canonical_item_id"],
                    "needs_attention": r["needs_attention"], "source": "auto",
                }

        overrides = conn.execute(
            """SELECT item_id, action, canonical_dataset_id, canonical_item_id
               FROM duplicate_overrides WHERE dataset_id = ?""",
            (dataset_id,),
        ).fetchall()
        for o in overrides:
            if o["action"] == "exclude":
                status[o["item_id"]] = {"kind": "excluded"}
            else:
                status[o["item_id"]] = {
                    "kind": "duplicate", "is_canonical": False,
                    "canonical_dataset_id": o["canonical_dataset_id"],
                    "canonical_item_id": o["canonical_item_id"],
                    "needs_attention": None, "source": "manual",
                }

        # A manual 'include' override can make a previously-unflagged item the
        # target of a new duplicate relationship -- that item needs to show up
        # as canonical here too, even though no automated run ever touched it.
        targeted = conn.execute(
            """SELECT DISTINCT canonical_item_id FROM duplicate_overrides
               WHERE action = 'include' AND canonical_dataset_id = ?""",
            (dataset_id,),
        ).fetchall()
        for t in targeted:
            status.setdefault(t["canonical_item_id"], {
                "kind": "duplicate", "is_canonical": True,
                "canonical_dataset_id": dataset_id, "canonical_item_id": t["canonical_item_id"],
                "needs_attention": None, "source": "manual",
            })

        for item_id, info in status.items():
            if info.get("kind") == "duplicate" and info["is_canonical"]:
                info["size"] = _count_duplicates_of(conn, dataset_id, item_id, run_id) + 1
    return status


def add_duplicate_override(dataset_id, item_id, action, canonical_dataset_id=None,
                            canonical_item_id=None, actor=DEFAULT_CODER, note=None):
    """Records a researcher's manual correction to duplicate detection.
    action='exclude': this item is NOT a duplicate -- overrides the automated
    run (a false positive from scripts/find_duplicates.py). action='include':
    this item IS a duplicate of (canonical_dataset_id, canonical_item_id),
    regardless of what -- or whether -- any run says (a false negative, or a
    pair the detector never compared). One row per (dataset_id, item_id); a
    later call replaces the prior override for that item. Never touches
    codes, segments, or queries/*.json -- only changes what
    classifier.build_corpus() and the duplicate flag treat as a duplicate."""
    if action not in ("exclude", "include"):
        raise ValueError("action must be 'exclude' or 'include'")
    if action == "include":
        if not (canonical_dataset_id and canonical_item_id):
            raise ValueError("canonical_dataset_id and canonical_item_id are required for action='include'")
        if (dataset_id, str(item_id)) == (canonical_dataset_id, str(canonical_item_id)):
            raise ValueError("an item cannot be a duplicate of itself")
    else:
        canonical_dataset_id = None
        canonical_item_id = None
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO duplicate_overrides
                 (dataset_id, item_id, action, canonical_dataset_id, canonical_item_id,
                  actor, created_at, note)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(dataset_id, item_id) DO UPDATE SET
                 action=excluded.action, canonical_dataset_id=excluded.canonical_dataset_id,
                 canonical_item_id=excluded.canonical_item_id, actor=excluded.actor,
                 created_at=excluded.created_at, note=excluded.note""",
            (dataset_id, item_id, action, canonical_dataset_id, canonical_item_id, actor, now, note),
        )
    log_activity(actor, "duplicate_override", {
        "dataset_id": dataset_id, "item_id": item_id, "action": action,
        "canonical_dataset_id": canonical_dataset_id, "canonical_item_id": canonical_item_id,
    })


def remove_duplicate_override(dataset_id, item_id, actor=DEFAULT_CODER):
    """Clears a manual override for (dataset_id, item_id), reverting it to
    whatever the latest automated run says (or "not a duplicate" if none, or
    if it was never part of any cluster)."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM duplicate_overrides WHERE dataset_id=? AND item_id=?",
            (dataset_id, item_id),
        )
    log_activity(actor, "duplicate_override_removed", {"dataset_id": dataset_id, "item_id": item_id})


def set_duplicate_cluster_canonical(run_id, dataset_id, item_id, actor=DEFAULT_CODER):
    """Manual override (scripts/find_duplicates.py --promote-canonical): makes
    (dataset_id, item_id) the canonical member of its cluster within run_id,
    demoting whichever member currently holds that role. No code migration --
    two independently-transcribed duplicates have no reliable segment-level
    correspondence to migrate between -- this only changes which item
    classifier.build_corpus() treats as canonical (and therefore keeps in the
    training corpus) going forward. Raises ValueError if the item isn't a
    member of any cluster in this run."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT cluster_id FROM duplicate_cluster_items WHERE run_id=? AND dataset_id=? AND item_id=?",
            (run_id, dataset_id, item_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"{dataset_id}:{item_id} is not a member of any cluster in run {run_id}")
        cluster_id = row["cluster_id"]
        conn.execute(
            "UPDATE duplicate_cluster_items SET is_canonical=0 WHERE run_id=? AND cluster_id=?",
            (run_id, cluster_id),
        )
        conn.execute(
            "UPDATE duplicate_cluster_items SET is_canonical=1 WHERE run_id=? AND dataset_id=? AND item_id=?",
            (run_id, dataset_id, item_id),
        )
        conn.execute(
            """UPDATE duplicate_clusters SET canonical_dataset_id=?, canonical_item_id=?, canonical_reason=?
               WHERE run_id=? AND cluster_id=?""",
            (dataset_id, item_id, "manual_override", run_id, cluster_id),
        )
    log_activity(actor, "duplicate_canonical_override", {
        "run_id": run_id, "dataset_id": dataset_id, "item_id": item_id, "cluster_id": cluster_id,
    })
