"""Exports the trained classifier as a full-corpus "topic model" artifact --
every segment's score against every trained theme, plus a per-theme "seed
pack" (top terms + best exemplars) -- for use in a separate, unrelated
pipeline (comparing against another topic-modeling method, or seeding a
guided/LLM-based one). This app never does that comparison itself; it only
produces a clean, exportable artifact of what its own classifier already
computed for the entire corpus.

Mirrors coding_store.activity_log's export_codebook() JSON+CSV-pair-per-table
idiom exactly, and is a pure, top-level, single-purpose module the same way
appendix_export.py/corpus_analytics.py are -- it only reads via coding_store,
never mutates anything.
"""
import csv
import json
from pathlib import Path

from coding_store import activity_log, model_runs, schema, themes

SEGMENT_SCORE_FIELDS = [
    "segment_id", "item_id", "dataset_id", "group_name", "person_name",
    "theme_id", "theme_name", "scheme_id", "parent_theme_id",
    "model_version", "engine", "score", "threshold", "above_threshold", "coded",
]
SEED_PACK_FIELDS = [
    "theme_id", "name", "description", "scheme_id", "parent_theme_id", "depth",
    "model_version", "engine", "n_pos", "threshold", "coverage_pct",
    "top_terms_positive", "exemplars",
]

MAX_SEED_EXEMPLARS = 20


def _engine_prefix(engine):
    return {"tfidf": "tfidf_v1_", "embed": "embed_v1_"}.get(engine, engine)


def _latest_run_for_engine(theme_id, engine):
    prefix = _engine_prefix(engine)
    runs = model_runs.list_model_runs(theme_id)  # already ORDER BY trained_at DESC
    return next((r for r in runs if r["model_version"].startswith(prefix)), None)


def export_full_corpus_topic_model(output_dir, scheme_id=None, model_version=None, engine="tfidf",
                                    actor=schema.DEFAULT_CODER):
    """Writes segment_topic_scores.json/.csv (one row per segment x theme,
    full corpus, full precision) and topic_seed_pack.json/.csv (one row per
    theme: top terms + top exemplars) into output_dir.

    scheme_id restricts to one scheme's themes (None = every scheme).
    model_version, if given, is used for every theme (reproducing exactly one
    past training pass); otherwise each theme's own latest run matching
    `engine` ("tfidf" or "embed") is used. A theme with no matching trained
    run is skipped from both files. Returns the list of paths written.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    theme_list = themes.list_themes(scheme_id=scheme_id)

    score_rows = []
    seed_rows = []
    for theme in theme_list:
        if model_version:
            run = next(
                (r for r in model_runs.list_model_runs(theme["theme_id"]) if r["model_version"] == model_version),
                None,
            )
        else:
            run = _latest_run_for_engine(theme["theme_id"], engine)
        if run is None:
            continue

        predictions = model_runs.get_predictions_for_theme(theme["theme_id"], run["model_version"])
        for p in predictions:
            score_rows.append({
                "segment_id": p["segment_id"], "item_id": p["item_id"], "dataset_id": p["dataset_id"],
                "group_name": p["group_name"], "person_name": p["person_name"],
                "theme_id": theme["theme_id"], "theme_name": theme["name"],
                "scheme_id": theme["scheme_id"], "parent_theme_id": theme["parent_theme_id"],
                "model_version": run["model_version"], "engine": engine, "score": p["score"],
                "threshold": run["threshold"], "above_threshold": p["score"] >= run["threshold"],
                "coded": bool(p["already_coded"]),
            })

        top_terms = model_runs.get_top_terms(run["model_version"], theme["theme_id"])
        exemplars = [
            {"segment_id": e["segment_id"], "text": e["text"], "score": e["score"], "coded": bool(e["already_coded"])}
            for e in model_runs.get_top_predictions(theme["theme_id"], run["model_version"], limit=MAX_SEED_EXEMPLARS)
        ]
        coverage = model_runs.get_corpus_coverage(
            theme["theme_id"], run["model_version"], run["threshold"], total_segments=len(predictions),
        )
        seed_rows.append({
            "theme_id": theme["theme_id"], "name": theme["name"], "description": theme["description"],
            "scheme_id": theme["scheme_id"], "parent_theme_id": theme["parent_theme_id"], "depth": theme["depth"],
            "model_version": run["model_version"], "engine": engine, "n_pos": run["n_pos"],
            "threshold": run["threshold"], "coverage_pct": coverage["coverage_pct"],
            "top_terms_positive": top_terms["positive"], "exemplars": exemplars,
        })

    paths = []
    for name, rows, fields, json_fields in (
        ("segment_topic_scores", score_rows, SEGMENT_SCORE_FIELDS, ()),
        ("topic_seed_pack", seed_rows, SEED_PACK_FIELDS, ("top_terms_positive", "exemplars")),
    ):
        json_path = output_dir / f"{name}.json"
        csv_path = output_dir / f"{name}.csv"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            csv_rows = rows
            if json_fields:
                # Nested fields (top_terms_positive/exemplars) don't fit a flat CSV cell --
                # same "rich in JSON, stringified in CSV" idiom query_api.write_export uses
                # for `turns`.
                csv_rows = [
                    {**row, **{jf: json.dumps(row[jf], ensure_ascii=False) for jf in json_fields}}
                    for row in rows
                ]
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(csv_rows)
        paths.extend([json_path, csv_path])

    activity_log.log_activity(actor, "topic_model_export", {
        "scheme_id": scheme_id, "model_version": model_version, "engine": engine,
        "themes_exported": len(seed_rows), "segment_rows": len(score_rows),
        "files": [p.name for p in paths],
    })
    return paths
