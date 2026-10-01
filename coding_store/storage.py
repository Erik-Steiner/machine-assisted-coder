"""Disk-footprint management for coding.db: opt-in pruning of old training runs'
per-segment scores, pinning runs to protect them, and compaction (VACUUM).

Why this exists: `predictions` is the one table whose size scales with
(segments x trained themes x training runs). Every pass writes one row per
in-scope segment for each theme it trains, and nothing ever removes old runs. On a
real project (2026-10-01) that was 4.40M rows from 10 runs, most of a 1.24 GB
coding.db: roughly 18 MB per trained theme per run, at about 72k in-scope
segments per theme. Pruning to the latest 2 runs per theme and then compacting
took that copy of the file down to 431 MB.

What pruning may touch: only `predictions` rows, and only for (model_version,
theme_id) pairs plan_prune() marks prunable. It never touches codes, negatives,
themes, or a run's summary record (model_runs metrics, model_top_terms,
model_run_params), so learning curves and the Web Appendix keep working for a
pruned run. Pruned scores can't be regenerated exactly (the codes they were
trained on have since changed), which is why a run can be pinned.

Nothing here runs automatically: training never triggers it, and every
deletion is a list of pairs the researcher confirmed, re-checked against
plan_prune() at delete time (see prune_predictions()).
"""
import shutil
from collections import defaultdict
from pathlib import Path

import paths
from . import activity_log, model_runs, schema

DEFAULT_KEEP_LATEST = 2

# Free-space safety margin for compact_database(). In WAL mode, VACUUM builds a
# compacted copy, then writes every page of it through the WAL before
# checkpointing it back, so the worst case needs about twice the live database
# size free at once, on top of the file that's already there.
VACUUM_SPACE_FACTOR = 2.0

# Pages are rarely 100% full; this inflates the per-row byte estimate (see
# _estimate_bytes_per_row()) to roughly what a page actually costs. Calibrated
# against the real project above: the estimate came out within ~10% of the
# file size divided by predictions rows.
PAGE_FILL_OVERHEAD = 1.15


# --- The deletion rule (pure; see tests/test_storage.py) ----------------------------

def plan_prune(runs, pinned_versions, keep_latest=DEFAULT_KEEP_LATEST):
    """Decide which (model_version, theme_id) pairs' scores may be pruned.

    runs: iterable of dicts with model_version, theme_id, trained_at, engine
          (model_runs rows -- one per theme a pass trained).
    pinned_versions: set of model_versions the researcher pinned.
    keep_latest: how many of each (theme, engine)'s most recent runs to keep;
          must be >= 1, so a theme's latest run per engine is always kept -- it's
          what every view and the Review queue read (see
          model_runs.get_latest_model_runs(engine=)).

    Returns (prunable, kept): prunable is a set of (model_version, theme_id);
    kept maps every other pair to its reason: "latest", "recent" (within
    keep_latest but not the newest), or "pinned". A pinned run is reported as
    "pinned" even if it's also latest/recent.

    Grouping by (theme, engine), not by theme alone: an embedding run must
    never push the theme's latest TF-IDF run out of the kept set, or vice
    versa. A model_version whose engine is unknown (None) forms its own group,
    so it gets the same protection rather than being lumped in with another
    engine's runs."""
    if keep_latest < 1:
        raise ValueError("keep_latest must be at least 1 -- each theme's latest run per engine is always kept")

    by_group = defaultdict(list)
    for r in runs:
        by_group[(r["theme_id"], r.get("engine"))].append(r)

    prunable, kept = set(), {}
    for group_runs in by_group.values():
        # Newest first; model_version breaks a trained_at tie deterministically.
        ordered = sorted(group_runs, key=lambda r: (r["trained_at"], r["model_version"]), reverse=True)
        for rank, r in enumerate(ordered):
            pair = (r["model_version"], r["theme_id"])
            if r["model_version"] in pinned_versions:
                kept[pair] = "pinned"
            elif rank == 0:
                kept[pair] = "latest"
            elif rank < keep_latest:
                kept[pair] = "recent"
            else:
                prunable.add(pair)
    return prunable, kept


# --- Pins ------------------------------------------------------------------------

def list_pins():
    """{model_version: {pinned_at, actor, note}}."""
    with schema.get_conn() as conn:
        rows = conn.execute("SELECT * FROM model_run_pins").fetchall()
    return {r["model_version"]: {"pinned_at": r["pinned_at"], "actor": r["actor"], "note": r["note"]}
            for r in rows}


def set_pin(model_version, pinned, actor=schema.DEFAULT_CODER, note=None):
    """Pin or unpin a whole training pass (every theme it trained). Pinning
    protects its scores from pruning; unpinning only removes that protection
    and deletes nothing. Both are logged."""
    with schema.get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM model_runs WHERE model_version=? LIMIT 1", (model_version,)
        ).fetchone()
        if not exists:
            raise ValueError(f"unknown model_version '{model_version}'")
        if pinned:
            conn.execute(
                """INSERT INTO model_run_pins (model_version, pinned_at, actor, note) VALUES (?,?,?,?)
                   ON CONFLICT(model_version) DO UPDATE SET note=excluded.note""",
                (model_version, schema._now(), actor, note),
            )
        else:
            conn.execute("DELETE FROM model_run_pins WHERE model_version=?", (model_version,))
    activity_log.log_activity(actor, "run_pin" if pinned else "run_unpin",
                              {"model_version": model_version, "note": note})


# --- Measuring --------------------------------------------------------------------

def _file_size(path):
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def database_files():
    """Byte sizes of coding.db and its WAL/shared-memory side files."""
    db = Path(schema.DB_PATH)
    return {
        "db_bytes": _file_size(db),
        "wal_bytes": _file_size(str(db) + "-wal"),
        "shm_bytes": _file_size(str(db) + "-shm"),
    }


def _page_stats(conn):
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return page_size, page_count, freelist


def _estimate_bytes_per_row(conn):
    """Approximate on-disk bytes per predictions row, including its two indexes.
    Python's bundled SQLite usually lacks the dbstat table that would give exact
    per-table sizes, so this sums average field widths (sampled) across the table
    row and both index entries, then applies PAGE_FILL_OVERHEAD. An estimate for
    choosing what to prune; the actual space freed is measured afterwards from
    freelist_count (see prune_predictions())."""
    row = conn.execute(
        """SELECT AVG(LENGTH(segment_id)) AS seg, AVG(LENGTH(theme_id)) AS theme,
                  AVG(LENGTH(model_version)) AS mv, AVG(LENGTH(created_at)) AS created,
                  AVG(oof_score IS NOT NULL) AS has_oof
           FROM (SELECT * FROM predictions LIMIT 20000)"""
    ).fetchone()
    if row["seg"] is None:
        return 0.0
    keys = row["seg"] + row["theme"] + row["mv"]
    table_row = 4 + 8 + keys + 8 + 8 * row["has_oof"] + row["created"]  # rowid+header, score, oof
    unique_index = 8 + keys + 4                                         # (segment, theme, version) + rowid
    theme_index = 6 + row["theme"] + row["mv"] + 4                      # (theme, version) + rowid
    return (table_row + unique_index + theme_index) * PAGE_FILL_OVERHEAD


def _prediction_counts(conn):
    """{(model_version, theme_id): row_count} -- served by idx_predictions_theme."""
    rows = conn.execute(
        "SELECT model_version, theme_id, COUNT(*) AS n FROM predictions GROUP BY theme_id, model_version"
    ).fetchall()
    return {(r["model_version"], r["theme_id"]): r["n"] for r in rows}


def _embedding_files():
    out = []
    emb_dir = Path(paths.EMBEDDINGS_DIR)
    if emb_dir.is_dir():
        for f in sorted(emb_dir.glob("*.npy")):
            out.append({"file": f.name, "bytes": _file_size(f)})
    return out


def storage_report(keep_latest=DEFAULT_KEEP_LATEST):
    """Everything the Model tab's Storage panel shows: file sizes, reusable free
    space, and every training pass with its per-theme row counts, estimated size,
    and plan_prune() status ("latest"/"recent"/"pinned"/"prunable", or
    "pruned" for a pair whose scores are already gone). Runs are grouped by
    model_version (one training pass), newest first. Read-only."""
    runs = model_runs.list_model_runs()
    pins = list_pins()
    prunable, kept = plan_prune(runs, set(pins), keep_latest=keep_latest)

    with schema.get_conn() as conn:
        counts = _prediction_counts(conn)
        bytes_per_row = _estimate_bytes_per_row(conn)
        page_size, page_count, freelist = _page_stats(conn)
        theme_names = {r["theme_id"]: r["name"] for r in conn.execute("SELECT theme_id, name FROM themes")}

    passes = {}
    for r in runs:
        mv = r["model_version"]
        p = passes.setdefault(mv, {
            "model_version": mv, "engine": r["engine"], "trained_at": r["trained_at"],
            "pinned": mv in pins, "pin_note": (pins.get(mv) or {}).get("note"),
            "themes": [], "rows": 0, "prunable_rows": 0,
        })
        pair = (mv, r["theme_id"])
        rows = counts.get(pair, 0)
        status = "prunable" if pair in prunable else kept[pair]
        if status == "prunable" and rows == 0:
            status = "pruned"
        p["themes"].append({
            "theme_id": r["theme_id"], "name": theme_names.get(r["theme_id"], r["theme_id"]),
            "rows": rows, "est_bytes": round(rows * bytes_per_row), "status": status,
        })
        p["rows"] += rows
        if status == "prunable":
            p["prunable_rows"] += rows
    for p in passes.values():
        p["est_bytes"] = round(p["rows"] * bytes_per_row)
        p["prunable_est_bytes"] = round(p["prunable_rows"] * bytes_per_row)

    known_pairs = {(r["model_version"], r["theme_id"]) for r in runs}
    orphan_rows = sum(n for pair, n in counts.items() if pair not in known_pairs)

    return {
        **database_files(),
        "page_size": page_size, "page_count": page_count,
        "free_bytes": freelist * page_size,  # reusable now, returned to disk only by compacting
        "predictions_rows": sum(counts.values()),
        "est_bytes_per_row": round(bytes_per_row, 1),
        "orphan_prediction_rows": orphan_rows,  # rows with no model_runs record; never pruned
        "keep_latest": keep_latest,
        "passes": sorted(passes.values(), key=lambda p: p["trained_at"], reverse=True),
        "embedding_files": _embedding_files(),
    }


# --- Acting -------------------------------------------------------------------------

def prune_predictions(pairs, keep_latest=DEFAULT_KEEP_LATEST, actor=schema.DEFAULT_CODER,
                      progress_callback=None):
    """Delete the per-segment scores for the given (model_version, theme_id)
    pairs. The request is re-checked against a fresh plan_prune() first: if any
    pair is latest/recent/pinned (or unknown), nothing at all is deleted and
    ValueError names the offending pairs. That way a stale UI, a pin added in
    another tab, or a hand-built request can't delete a protected run.

    Each pair is deleted in its own transaction, so a crash midway leaves every
    pair either fully pruned or untouched. Logged to activity_log. Returns
    {pairs, rows_deleted, freed_bytes}; freed_bytes is the measured growth in
    reusable free pages (the file itself only shrinks after compact_database())."""
    pairs = sorted({(p[0], p[1]) for p in pairs})
    if not pairs:
        raise ValueError("nothing selected to prune")
    runs = model_runs.list_model_runs()
    prunable, kept = plan_prune(runs, set(list_pins()), keep_latest=keep_latest)
    refused = [
        {"model_version": mv, "theme_id": tid, "reason": kept.get((mv, tid), "unknown run")}
        for mv, tid in pairs if (mv, tid) not in prunable
    ]
    if refused:
        raise ValueError(f"refusing to prune protected or unknown run(s): {refused}")

    with schema.get_conn() as conn:
        page_size, _, free_before = _page_stats(conn)

    rows_deleted = 0
    for i, (mv, tid) in enumerate(pairs):
        if progress_callback:
            progress_callback({"index": i, "total": len(pairs), "model_version": mv, "theme_id": tid})
        with schema.get_conn() as conn:
            cur = conn.execute("DELETE FROM predictions WHERE model_version=? AND theme_id=?", (mv, tid))
            rows_deleted += cur.rowcount

    with schema.get_conn() as conn:
        _, _, free_after = _page_stats(conn)
    freed_bytes = max(0, free_after - free_before) * page_size

    activity_log.log_activity(actor, "predictions_prune", {
        "pairs": [{"model_version": mv, "theme_id": tid} for mv, tid in pairs],
        "rows_deleted": rows_deleted, "freed_bytes": freed_bytes, "keep_latest": keep_latest,
    })
    return {"pairs": len(pairs), "rows_deleted": rows_deleted, "freed_bytes": freed_bytes}


def compaction_check():
    """{ok, required_bytes, available_bytes, ...} -- whether there's enough free
    disk space to VACUUM safely. See VACUUM_SPACE_FACTOR."""
    files = database_files()
    with schema.get_conn() as conn:
        page_size, page_count, freelist = _page_stats(conn)
    live_bytes = (page_count - freelist) * page_size
    required = round(live_bytes * VACUUM_SPACE_FACTOR)
    available = shutil.disk_usage(Path(schema.DB_PATH).parent).free
    return {
        "ok": available >= required, "required_bytes": required, "available_bytes": available,
        "live_bytes": live_bytes, "reclaimable_bytes": freelist * page_size, **files,
    }


def compact_database(actor=schema.DEFAULT_CODER):
    """VACUUM coding.db so space freed by pruning goes back to the disk, then
    checkpoint and truncate the WAL. Refuses (ValueError) when
    compaction_check() says free space is short. Locks the whole database while
    it runs, so the caller should stop other writers first (viewer_server.py
    refuses to start training while it runs). Logged. Returns sizes before/after."""
    check = compaction_check()
    if not check["ok"]:
        raise ValueError(
            f"not enough free disk space to compact safely: needs about "
            f"{check['required_bytes'] / 1e9:.2f} GB free, {check['available_bytes'] / 1e9:.2f} GB available"
        )
    before = database_files()
    with schema.get_conn() as conn:
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    after = database_files()
    result = {
        "db_bytes_before": before["db_bytes"] + before["wal_bytes"],
        "db_bytes_after": after["db_bytes"] + after["wal_bytes"],
    }
    result["reclaimed_bytes"] = max(0, result["db_bytes_before"] - result["db_bytes_after"])
    activity_log.log_activity(actor, "database_compact", result)
    return result
