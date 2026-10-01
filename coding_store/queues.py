"""Review queues (roadmap B1): which passages to show the researcher next for one
theme, from that theme's latest run of one engine. Each mode answers a different
question in the code -> train -> review loop:

  top        Highest-scoring passages not yet decided for the theme -- "find more".
  uncertain  Undecided passages scoring nearest the run's threshold -- the ones the
             model is least sure about, so deciding them teaches it the most.
  audit      A random sample of undecided passages above the threshold, in a fixed
             order per run -- what the researcher accepts here estimates the model's
             real precision (roadmap C2 builds the estimate from these decisions).
  suspect    Passages already decided where the model disagrees most: coded passages
             with the lowest out-of-fold score, and judged negatives with the highest.
             Uses oof_score (A3) -- a model that never saw the passage -- because the
             final model has memorized its own training labels. Possible miscodes, or
             a theme whose definition has drifted.
  confusion  Passages coded with this theme that score at or above threshold for a
             different theme in the same scheme (excluding its own ancestors and
             sub-codes, which overlap by design) and aren't explicitly decided for that
             theme (coded with it, or rejected for it) -- where two themes' boundaries
             may blur. Implicit judgments (a "fully coded" mark) don't exclude a
             passage here: a marked interview's coded passages are all implicitly "not
             the other themes", and those are exactly the boundary cases worth a look.

"Decided" uses the effective-label rules (coding_store/labels.py): effectively
coded, or a judged negative (explicit rejection, or reviewed "fully coded" / "none
of these" after the theme existed). Read-only; depends on schema, labels,
model_runs, segments, codes, themes.
"""
import random

from . import codes, labels, model_runs, schema, segments, themes

QUEUE_MODES = ("top", "uncertain", "audit", "suspect", "confusion")
DEFAULT_PAGE_SIZE = 25


def order_queue(mode, rows, threshold, positives, negatives, seed=""):
    """Pure ordering rule for the single-theme modes (everything but confusion).
    rows: [{segment_id, score, oof_score}] -- one theme's predictions from one run.
    positives/negatives: segment_id sets decided for the theme (see module docstring).
    Returns the ordered rows, each with a "label" key for suspect mode
    ("positive"/"negative"), and nothing a mode shouldn't show."""
    decided = positives | negatives
    if mode == "top":
        out = [r for r in rows if r["segment_id"] not in decided]
        return sorted(out, key=lambda r: (-r["score"], r["segment_id"]))
    if mode == "uncertain":
        out = [r for r in rows if r["segment_id"] not in decided]
        return sorted(out, key=lambda r: (abs(r["score"] - threshold), r["segment_id"]))
    if mode == "audit":
        out = sorted((r for r in rows if r["segment_id"] not in decided and r["score"] >= threshold),
                     key=lambda r: r["segment_id"])
        random.Random(seed).shuffle(out)  # fixed per (run, theme): pages stay stable
        return out
    if mode == "suspect":
        out = []
        for r in rows:
            if r.get("oof_score") is None:
                continue  # not fitted on in that run -> no honest score
            if r["segment_id"] in positives:
                out.append({**r, "label": "positive", "disagreement": 1.0 - r["oof_score"]})
            elif r["segment_id"] in negatives:
                out.append({**r, "label": "negative", "disagreement": r["oof_score"]})
        return sorted(out, key=lambda r: (-r["disagreement"], r["segment_id"]))
    raise ValueError(f"unknown queue mode '{mode}' -- expected one of {QUEUE_MODES}")


def _prediction_rows(theme_id, model_version):
    with schema.get_conn() as conn:
        rows = conn.execute(
            "SELECT segment_id, score, oof_score FROM predictions WHERE theme_id=? AND model_version=?",
            (theme_id, model_version),
        ).fetchall()
    return [dict(r) for r in rows]


def _confusion_rows(theme, engine, positives):
    """[{segment_id, score, other_theme_id, other_score, other_model_version}] for
    passages effectively coded with `theme` that score >= threshold for another
    theme in its scheme, outside its own lineage/subtree, not explicitly decided for
    that theme (coded or rejected -- see the module docstring for why marks don't count).
    One row per passage: its highest-scoring other theme. Sorted by that score."""
    related = labels.subtree_theme_ids(theme["theme_id"]) | labels.lineage_theme_ids(theme["theme_id"])
    latest = model_runs.get_latest_model_runs(engine=engine)
    best = {}
    for other in themes.list_themes(scheme_id=theme["scheme_id"]):
        if other["theme_id"] in related or other["theme_id"] not in latest:
            continue
        run = latest[other["theme_id"]]
        other_decided = (labels.effective_positive_segment_ids(other["theme_id"])
                         | labels.effective_rejected_segment_ids(other["theme_id"]))
        for r in _prediction_rows(other["theme_id"], run["model_version"]):
            sid = r["segment_id"]
            if sid not in positives or sid in other_decided or r["score"] < run["threshold"]:
                continue
            if sid not in best or r["score"] > best[sid]["other_score"]:
                best[sid] = {"segment_id": sid, "other_theme_id": other["theme_id"],
                             "other_score": r["score"], "other_model_version": run["model_version"]}
    return sorted(best.values(), key=lambda r: (-r["other_score"], r["segment_id"]))


def get_queue(theme_id, mode, engine="tfidf", offset=0, limit=DEFAULT_PAGE_SIZE):
    """One page of a review queue. Returns {theme_id, mode, engine, model_version,
    threshold, total, offset, limit, items} -- or the same with model_version None and
    no items when the theme has no run for that engine yet. Each item is a full
    segment row plus score/oof_score (this theme, this run), theme_ids /
    rejected_theme_ids (current codes and rejections, for the chips), and mode
    extras: label for suspect; other_theme_id/other_score for confusion."""
    if mode not in QUEUE_MODES:
        raise ValueError(f"unknown queue mode '{mode}' -- expected one of {QUEUE_MODES}")
    theme = themes.get_theme(theme_id)
    if theme is None:
        raise ValueError(f"unknown theme '{theme_id}'")
    run = model_runs.get_latest_model_runs(engine=engine).get(theme_id)
    empty = {"theme_id": theme_id, "mode": mode, "engine": engine, "model_version": None,
             "threshold": None, "total": 0, "offset": offset, "limit": limit, "items": []}
    if run is None:
        return empty

    rows = _prediction_rows(theme_id, run["model_version"])
    positives = labels.effective_positive_segment_ids(theme_id)
    if mode == "confusion":
        ordered = _confusion_rows(theme, engine, positives)
        by_id = {r["segment_id"]: r for r in rows}
        for r in ordered:
            own = by_id.get(r["segment_id"], {})
            r.update(score=own.get("score"), oof_score=own.get("oof_score"))
    else:
        negatives = labels.reviewed_negative_segment_ids(theme_id)
        ordered = order_queue(mode, rows, run["threshold"], positives, negatives,
                              seed=f"{run['model_version']}:{theme_id}")

    page = ordered[offset:offset + limit]
    seg_rows = {s["segment_id"]: s for s in segments.get_segments_by_ids([r["segment_id"] for r in page])}
    page_ids = list(seg_rows)
    codes_by = codes.get_codes_for_segment_ids(page_ids)
    rejected_by = codes.get_negatives_for_segment_ids(page_ids)
    items = []
    for r in page:
        seg = seg_rows.get(r["segment_id"])
        if seg is None:
            continue  # prediction for a segment no longer in coding.db
        items.append({**seg, **r, "theme_ids": codes_by.get(r["segment_id"], []),
                      "rejected_theme_ids": rejected_by.get(r["segment_id"], [])})
    return {**empty, "model_version": run["model_version"], "threshold": run["threshold"],
            "total": len(ordered), "items": items}
