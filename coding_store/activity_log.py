"""Web Appendix subsystem: the cross-cutting research-action log (activity_log
table), the hyperparameters behind each training pass (model_run_params
table), and the codebook backup/publication export. Grouped together because
all three exist for the same audience -- a citable methodological record --
even though export_codebook doesn't touch activity_log/model_run_params
itself; see viewer_server.py's module docstring, which lists "codebook
exports" as one of the Web Appendix log's own activity types.

No dependency on any other coding_store submodule besides schema -- themes.py,
duplicates.py, and dataset_status.py import this one for log_activity(), not
the other way around.
"""
import csv
import json
from pathlib import Path

from . import schema


def log_activity(actor, action_type, details=None):
    """Append one row to the cross-cutting research-action log. `details` is any
    JSON-serializable dict describing what happened; shape depends on action_type
    (see call sites in viewer_server.py/classifier.py). Never raises on a bad
    `details` value other than a real serialization error -- this is telemetry
    for the Web Appendix tab, not something that should block the action it's
    logging if it fails."""
    with schema.get_conn() as conn:
        conn.execute(
            "INSERT INTO activity_log (ts, actor, action_type, details_json) VALUES (?,?,?,?)",
            (schema._now(), actor, action_type, json.dumps(details or {})),
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
    with schema.get_conn() as conn:
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


def save_model_run_params(model_version, params, trained_at):
    """One row per training pass: the shared hyperparameters used, since
    model_runs' per-theme metrics say nothing about how they were produced."""
    with schema.get_conn() as conn:
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
    with schema.get_conn() as conn:
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
    with schema.get_conn() as conn:
        row = conn.execute(
            "SELECT params_json, trained_at FROM model_run_params WHERE model_version=?",
            (model_version,),
        ).fetchone()
    if row is None:
        return None
    return {"params": json.loads(row["params_json"]), "trained_at": row["trained_at"]}


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

    with schema.get_conn() as conn:
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
