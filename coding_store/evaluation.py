"""Review -> Evaluate (docs/CODEBOOK_PLAN.md U6): how well one run does against the
researcher's gold, per theme, with intervals, next to a second run for comparison.

What counts as gold, until C3's blind gold sample exists: every passage in an interview
whose "fully coded" mark counts as read in full (reviewed.READ_IN_FULL_STATUSES:
confirmed or interactive), for the themes that mark covers -- those that existed when it
was made (labels.reviewed_negative_segment_ids()'s rule). Inside such an interview a
passage coded with the theme or a sub-code (labels.effective_positive_segment_ids()) is
positive, and every other passage is a judged negative. Unconfirmed bulk marks don't
count: many of those interviews were never read end to end. This gold isn't blind or
random -- the researcher chose which interviews to read -- and the tab says so.

Why turns, not segments: every engine must be compared on the same items (CODEBOOK_PLAN
L5, U6), and a topic method codes turns. A segment can be a fragment of a turn or several
short turns merged, so segments roll up to turns via K6 (segmentation.segment_turn_indices,
reached through a turn lookup the caller passes in): a turn is positive if any of its
segments is (stage 0's rule; inside a covered interview every passage is labelled, so
"negative only if every segment is" is the same thing), and it scores the highest of its
segments' scores. Only segments the run scored count, so interviewer turns and fragments
too short to score drop out, as they do from training. When a record can't be loaded the
segment stands in for its turn, and the response counts how often that happened.

Why the scores are honest: each segment's out-of-fold score (oof_score) when the run
fitted on it, else its full-fit score -- a segment the run didn't fit on was never seen by
it. The threshold is the run's own (model_runs.threshold), which training picked to
maximize F1 on those same out-of-fold scores, so the point estimates are mildly optimistic.

Read-only. The pure functions (wilson, prf, average_precision, roll_up_to_turns,
calibration_bands) hold the arithmetic; evaluate_run() gathers the facts from coding.db.
"""
import math

from . import labels, model_runs, reviewed, schema, themes

CALIBRATION_EDGES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)


def wilson(k, n, z=1.96):
    """95% Wilson score interval for k successes in n trials, or None when n is 0."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def prf(predicted, gold):
    """Precision, recall and F1 of parallel 0/1 lists, with Wilson intervals; a ratio
    with nothing to divide by is None, never 0."""
    tp = sum(1 for p, g in zip(predicted, gold) if p and g)
    fp = sum(1 for p, g in zip(predicted, gold) if p and not g)
    fn = sum(1 for p, g in zip(predicted, gold) if not p and g)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision is not None and recall is not None and precision + recall else None)
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": precision, "precision_ci": wilson(tp, tp + fp),
            "recall": recall, "recall_ci": wilson(tp, tp + fn), "f1": f1}


def average_precision(scores, gold):
    """Area under the precision-recall curve as average precision (sklearn's definition:
    precision at each distinct score, weighted by the recall gained there). None without
    positives."""
    n_pos = sum(1 for g in gold if g)
    if not n_pos:
        return None
    pairs = sorted(zip(scores, gold), key=lambda x: -x[0])
    ap, tp, seen, i = 0.0, 0, 0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:  # tied scores move together
            j += 1
        gained = sum(1 for _, g in pairs[i:j] if g)
        tp += gained
        seen = j
        ap += (gained / n_pos) * (tp / seen)
        i = j
    return ap


def roll_up_to_turns(segments):
    """segments: [{segment_id, dataset_id, item_id, turn_indices, positive, score}] for one
    theme. Returns [{key, positive, score, n_segments, by_segment}]: one unit per source
    turn, positive if any of its segments is, scored by the highest of them. A segment
    spanning several merged turns counts toward each. A segment with no turn_indices (its
    record couldn't be loaded) is its own unit, with by_segment True."""
    units = {}
    for s in segments:
        if s["turn_indices"]:
            keys = [("turn", s["dataset_id"], str(s["item_id"]), i) for i in s["turn_indices"]]
        else:
            keys = [("segment", s["segment_id"])]
        for key in keys:
            u = units.get(key)
            if u is None:
                u = units[key] = {"key": key, "positive": False, "score": s["score"], "n_segments": 0,
                                  "by_segment": key[0] == "segment"}
            u["positive"] = u["positive"] or bool(s["positive"])
            u["score"] = max(u["score"], s["score"])
            u["n_segments"] += 1
    return list(units.values())


def calibration_bands(scores, gold, edges=CALIBRATION_EDGES):
    """[{band: "0.2–0.4", n, share_positive}] -- how often a unit in each score band is
    really positive. The last band includes 1.0."""
    out = []
    for lo, hi in zip(edges, edges[1:]):
        last = hi == edges[-1]
        hits = [g for s, g in zip(scores, gold) if lo <= s < hi or (last and s == hi)]
        out.append({"band": f"{lo:.1f}–{hi:.1f}", "n": len(hits),
                    "share_positive": (sum(1 for g in hits if g) / len(hits)) if hits else None})
    return out


def _metrics(units, threshold):
    gold = [u["positive"] for u in units]
    scores = [u["score"] for u in units]
    out = prf([s >= threshold for s in scores], gold)
    out["pr_auc"] = average_precision(scores, gold)
    out["threshold"] = threshold
    return out


# --- Gathering the facts -----------------------------------------------------------------


def gold_items_by_theme(scheme_id, theme_rows):
    """{theme_id: {(dataset_id, item_id)}}: the read-in-full interviews whose mark covers
    each theme, i.e. was made at or after the theme was created (labels.py's rule)."""
    with schema.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM item_reviews WHERE scheme_id=? AND unmarked_at IS NULL ORDER BY marked_at",
            (scheme_id,),
        ).fetchall()
    latest = {}
    for r in rows:  # the newest active mark wins, as in reviewed.get_item_review()
        latest[(r["dataset_id"], str(r["item_id"]))] = dict(r)
    read_in_full = {k: m for k, m in latest.items() if reviewed.mark_status(m) in reviewed.READ_IN_FULL_STATUSES}
    return {t["theme_id"]: {k for k, m in read_in_full.items() if m["marked_at"] >= t["created_at"]}
            for t in theme_rows}


def _segments_of_items(items):
    """[{segment_id, dataset_id, item_id}] for every segment of the given interviews."""
    out = []
    by_dataset = {}
    for dataset_id, item_id in items:
        by_dataset.setdefault(dataset_id, []).append(item_id)
    with schema.get_conn() as conn:
        for dataset_id, item_ids in by_dataset.items():
            for i in range(0, len(item_ids), schema.SQLITE_MAX_VARIABLES - 1):
                batch = item_ids[i:i + schema.SQLITE_MAX_VARIABLES - 1]
                # Plain s.item_id IN (...) for the same reason as labels.py: older databases
                # declare item_id INTEGER, and SQLite's affinity rules still match text ids.
                rows = conn.execute(
                    f"""SELECT segment_id, dataset_id, CAST(item_id AS TEXT) AS item_id FROM segments
                        WHERE dataset_id=? AND item_id IN ({','.join('?' * len(batch))})""",
                    (dataset_id, *batch),
                ).fetchall()
                out.extend(dict(r) for r in rows)
    return out


def _scores(model_version, theme_id, segment_ids):
    """{segment_id: honest score} for the run: oof_score when it fitted on the segment,
    else the full-fit score."""
    out = {}
    ids = sorted(segment_ids)
    with schema.get_conn() as conn:
        for i in range(0, len(ids), schema.SQLITE_MAX_VARIABLES - 2):
            batch = ids[i:i + schema.SQLITE_MAX_VARIABLES - 2]
            rows = conn.execute(
                f"""SELECT segment_id, score, oof_score FROM predictions
                    WHERE model_version=? AND theme_id=? AND segment_id IN ({','.join('?' * len(batch))})""",
                (model_version, theme_id, *batch),
            ).fetchall()
            for r in rows:
                out[r["segment_id"]] = r["oof_score"] if r["oof_score"] is not None else r["score"]
    return out


def _has_scores(model_version, theme_id):
    with schema.get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM predictions WHERE model_version=? AND theme_id=? LIMIT 1", (model_version, theme_id),
        ).fetchone() is not None


def _run_rows(model_version):
    """{theme_id: model_runs row} for one pass."""
    with schema.get_conn() as conn:
        rows = conn.execute("SELECT * FROM model_runs WHERE model_version=?", (model_version,)).fetchall()
    return {r["theme_id"]: dict(r) for r in rows}


def evaluate_run(scheme_id, model_version, compare_version=None, turn_lookup=None):
    """The Evaluate tab's numbers for one run of a scheme (see the module docstring).
    turn_lookup: an object with get(segment_id, dataset_id, item_id) -> turn indices, such
    as codebook_export.TurnIndexLookup; None evaluates segments instead. Raises ValueError
    when the run trained none of the scheme's themes."""
    theme_rows = [t for t in themes.list_themes(include_archived=True, scheme_id=scheme_id)
                  if not t.get("merged_into")]
    run = _run_rows(model_version)
    trained = [t for t in theme_rows if t["theme_id"] in run]
    if not trained:
        raise ValueError(f"run '{model_version}' trained none of this scheme's themes")
    compare = _run_rows(compare_version) if compare_version else {}

    gold_items = gold_items_by_theme(scheme_id, trained)
    all_items = set().union(*gold_items.values())
    segments = _segments_of_items(sorted(all_items))
    turns_of = {}
    for s in segments:
        turns_of[s["segment_id"]] = turn_lookup.get(s["segment_id"], s["dataset_id"], s["item_id"]) \
            if turn_lookup is not None else []

    out_themes, pooled_scores, pooled_gold = [], [], []
    units_by_segment = 0
    for t in trained:
        tid = t["theme_id"]
        entry = {"theme_id": tid, "name": t["name"], "status": t["status"],
                 "gold_interviews": len(gold_items[tid]), "run": None, "compare": None, "note": None}
        out_themes.append(entry)
        if not gold_items[tid]:
            entry["note"] = "no_gold"
            continue
        if not _has_scores(model_version, tid):
            entry["note"] = "scores_pruned"
            continue
        theme_segments = [s for s in segments if (s["dataset_id"], s["item_id"]) in gold_items[tid]]
        positives = labels.effective_positive_segment_ids(tid)
        scores = _scores(model_version, tid, [s["segment_id"] for s in theme_segments])
        rows = [{**s, "turn_indices": turns_of[s["segment_id"]], "positive": s["segment_id"] in positives,
                 "score": scores[s["segment_id"]]} for s in theme_segments if s["segment_id"] in scores]
        units = roll_up_to_turns(rows)
        if not units:
            entry["note"] = "no_scored_gold"
            continue
        units_by_segment += sum(1 for u in units if u["by_segment"])
        entry["n_units"] = len(units)
        entry["n_pos"] = sum(1 for u in units if u["positive"])
        entry["run"] = _metrics(units, run[tid]["threshold"])
        pooled_scores += [u["score"] for u in units]
        pooled_gold += [u["positive"] for u in units]

        if tid in compare and _has_scores(compare_version, tid):
            # The same gold passages, scored by the other run. Passages it didn't score (outside
            # its corpus) drop out of its numbers only; its n_units says how many remained.
            c_scores = _scores(compare_version, tid, [r["segment_id"] for r in rows])
            c_rows = [{**r, "score": c_scores[r["segment_id"]]} for r in rows if r["segment_id"] in c_scores]
            c_units = roll_up_to_turns(c_rows)
            if c_units:
                entry["compare"] = _metrics(c_units, compare[tid]["threshold"])
                entry["compare"]["n_units"] = len(c_units)

    return {
        "scheme_id": scheme_id,
        "model_version": model_version,
        "engine": model_runs.engine_of_version(model_version),
        "trained_at": next(iter(run.values()))["trained_at"],
        "compare_version": compare_version if compare else None,
        "compare_engine": model_runs.engine_of_version(compare_version) if compare else None,
        "unit": "turn" if turn_lookup is not None else "segment",
        "units_by_segment": units_by_segment,
        "gold": {
            "read_in_full_interviews": len(all_items),
            "unconfirmed_marks": len(reviewed.unconfirmed_marks(scheme_id)),
        },
        "themes": out_themes,
        "calibration": calibration_bands(pooled_scores, pooled_gold),
    }
