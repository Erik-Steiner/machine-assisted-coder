"""Whole-dataset ("corpus") inclusion state, set from the Coding tab's
Datasets panel. Absence of a row means the dataset is active/included --
same "mark-only" idiom as duplicates.py's overrides. Depends on activity_log
(every status change is logged), not on any other coding_store submodule.
"""
from . import activity_log, schema


def set_dataset_status(dataset_id, status, actor=schema.DEFAULT_CODER, note=None):
    """Flags a whole dataset 'excluded' (kept visible in Browse/Coding, dropped from
    classifier.build_corpus()) or 'unloaded' (hidden everywhere, restorable later --
    see viewer_server.get_dataset()). One row per dataset_id; a later call replaces
    the prior status."""
    if status not in ("excluded", "unloaded"):
        raise ValueError("status must be 'excluded' or 'unloaded'")
    now = schema._now()
    with schema.get_conn() as conn:
        conn.execute(
            """INSERT INTO dataset_status (dataset_id, status, actor, updated_at, note)
               VALUES (?,?,?,?,?)
               ON CONFLICT(dataset_id) DO UPDATE SET
                 status=excluded.status, actor=excluded.actor,
                 updated_at=excluded.updated_at, note=excluded.note""",
            (dataset_id, status, actor, now, note),
        )
    activity_log.log_activity(actor, "dataset_excluded" if status == "excluded" else "dataset_unloaded",
                 {"dataset_id": dataset_id, "note": note})


def clear_dataset_status(dataset_id, actor=schema.DEFAULT_CODER):
    """Reverts a dataset to active (included everywhere, in training) by clearing
    any 'excluded'/'unloaded' flag."""
    with schema.get_conn() as conn:
        conn.execute("DELETE FROM dataset_status WHERE dataset_id=?", (dataset_id,))
    activity_log.log_activity(actor, "dataset_restored", {"dataset_id": dataset_id})


def get_dataset_status(dataset_id):
    with schema.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM dataset_status WHERE dataset_id=?", (dataset_id,)
        ).fetchone()
    return dict(row) if row else None


def list_dataset_statuses():
    """{dataset_id: {status, actor, updated_at, note}} for every flagged dataset --
    datasets with no row are active/included."""
    with schema.get_conn() as conn:
        rows = conn.execute("SELECT * FROM dataset_status").fetchall()
    return {r["dataset_id"]: dict(r) for r in rows}


def excluded_dataset_ids():
    """dataset_ids currently excluded from training -- 'excluded' and 'unloaded'
    both count, since an unloaded dataset shouldn't feed the classifier either.
    Consulted by classifier.build_corpus()."""
    with schema.get_conn() as conn:
        rows = conn.execute(
            "SELECT dataset_id FROM dataset_status WHERE status IN ('excluded','unloaded')"
        ).fetchall()
    return {r["dataset_id"] for r in rows}
