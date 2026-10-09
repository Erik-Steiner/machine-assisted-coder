"""The Train tab's Runs page (docs/CODEBOOK_PLAN.md U2): one row per training pass
(model_version) for a scheme, across engines, newest first -- what it was trained on
(engine, negative mode, the codebook version, labels per theme) and how it did
(per-theme metrics plus their averages), and whether it's pinned.

Why per pass rather than per theme: a pass is what a researcher picks, compares, pins
and cites. model_runs stores one row per (pass, theme); this module only groups and
annotates those rows. Read-only.

A pass belongs to a scheme if it trained any of the scheme's themes, archived and merged
ones included, so a run doesn't vanish from history when a theme is retired. The codebook
version comes from the pass's model_run_params (classifier.training_params()); passes
from before it was recorded have none, and the page says "not recorded" rather than
guessing.
"""
import json

from . import model_runs, schema, storage, themes

METRIC_KEYS = ("precision", "recall", "f1", "pr_auc")


def _params_by_version(model_versions):
    """{model_version: params dict} from model_run_params, for the given passes."""
    out = {}
    versions = sorted(model_versions)
    batch_size = schema.SQLITE_MAX_VARIABLES
    with schema.get_conn() as conn:
        for i in range(0, len(versions), batch_size):
            batch = versions[i:i + batch_size]
            rows = conn.execute(
                f"SELECT model_version, params_json FROM model_run_params "
                f"WHERE model_version IN ({','.join('?' * len(batch))})",
                batch,
            ).fetchall()
            for r in rows:
                try:
                    out[r["model_version"]] = json.loads(r["params_json"])
                except (TypeError, ValueError):
                    out[r["model_version"]] = {}
    return out


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def list_runs(scheme_id):
    """Every training pass that trained one of the scheme's themes, newest first:
    {model_version, engine, trained_at, negative_mode, codebook_version (None when not
    recorded), pinned, pin_note, n_themes, mean_<metric> for METRIC_KEYS, themes: [{theme_id,
    name, status, n_pos, n_neg, precision, recall, f1, pr_auc, threshold}]}. Themes in a
    pass are in codebook order."""
    scheme_themes = themes.list_themes(include_archived=True, scheme_id=scheme_id)
    order = {t["theme_id"]: i for i, t in enumerate(scheme_themes)}
    by_id = {t["theme_id"]: t for t in scheme_themes}
    rows = [r for r in model_runs.list_model_runs() if r["theme_id"] in by_id]
    params = _params_by_version({r["model_version"] for r in rows})
    pins = storage.list_pins()

    passes = {}
    for r in rows:
        mv = r["model_version"]
        p = passes.get(mv)
        if p is None:
            versions = (params.get(mv) or {}).get("codebook_versions") or {}
            p = passes[mv] = {
                "model_version": mv, "engine": r["engine"], "trained_at": r["trained_at"],
                "negative_mode": r["negative_mode"], "codebook_version": versions.get(scheme_id),
                "pinned": mv in pins, "pin_note": (pins.get(mv) or {}).get("note"), "themes": [],
            }
        t = by_id[r["theme_id"]]
        p["themes"].append({
            "theme_id": r["theme_id"], "name": t["name"], "status": t["status"],
            "n_pos": r["n_pos"], "n_neg": r["n_neg"], "threshold": r["threshold"],
            **{k: r[k] for k in METRIC_KEYS},
        })
    for p in passes.values():
        p["themes"].sort(key=lambda t: order[t["theme_id"]])
        p["n_themes"] = len(p["themes"])
        for k in METRIC_KEYS:
            p[f"mean_{k}"] = _mean([t[k] for t in p["themes"]])
    return sorted(passes.values(), key=lambda p: p["trained_at"], reverse=True)
