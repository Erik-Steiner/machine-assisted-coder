"""Near-duplicate detection (scripts/find_duplicates.py) -- the same real-world
speech independently re-transcribed by several outlets, each landing under
its own item_id. Mark-only: nothing here ever deletes/hides a queries/*.json
record or touches `codes`. Depends on activity_log (every override/promotion
is logged) but not on segments.py/codes.py -- its own SQL joins directly
against the segments/codes tables where needed (get_active_code_counts,
get_duplicate_coded_status).
"""
import json

from . import activity_log, schema


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
    with schema.get_conn() as conn:
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
    with schema.get_conn() as conn:
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
    with schema.get_conn() as conn:
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
    with schema.get_conn() as conn:
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
    with schema.get_conn() as conn:
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
    with schema.get_conn() as conn:
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
                            canonical_item_id=None, actor=schema.DEFAULT_CODER, note=None):
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
    now = schema._now()
    with schema.get_conn() as conn:
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
    activity_log.log_activity(actor, "duplicate_override", {
        "dataset_id": dataset_id, "item_id": item_id, "action": action,
        "canonical_dataset_id": canonical_dataset_id, "canonical_item_id": canonical_item_id,
    })


def remove_duplicate_override(dataset_id, item_id, actor=schema.DEFAULT_CODER):
    """Clears a manual override for (dataset_id, item_id), reverting it to
    whatever the latest automated run says (or "not a duplicate" if none, or
    if it was never part of any cluster)."""
    with schema.get_conn() as conn:
        conn.execute(
            "DELETE FROM duplicate_overrides WHERE dataset_id=? AND item_id=?",
            (dataset_id, item_id),
        )
    activity_log.log_activity(actor, "duplicate_override_removed", {"dataset_id": dataset_id, "item_id": item_id})


def set_duplicate_cluster_canonical(run_id, dataset_id, item_id, actor=schema.DEFAULT_CODER):
    """Manual override (scripts/find_duplicates.py --promote-canonical): makes
    (dataset_id, item_id) the canonical member of its cluster within run_id,
    demoting whichever member currently holds that role. No code migration --
    two independently-transcribed duplicates have no reliable segment-level
    correspondence to migrate between -- this only changes which item
    classifier.build_corpus() treats as canonical (and therefore keeps in the
    training corpus) going forward. Raises ValueError if the item isn't a
    member of any cluster in this run."""
    with schema.get_conn() as conn:
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
    activity_log.log_activity(actor, "duplicate_canonical_override", {
        "run_id": run_id, "dataset_id": dataset_id, "item_id": item_id, "cluster_id": cluster_id,
    })
