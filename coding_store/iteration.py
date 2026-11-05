"""Where a scheme stands in the codebook -> train -> review -> decide cycle
(docs/CODEBOOK_PLAN.md U3, U4): the round number, each step's state, and the reasons, most
important first, each naming the tab that resolves it. Backs GET /api/iteration/status and
the one-line cycle strip on the Train and Review pages.

Advisory only, like the codebook's completeness checks: nothing here blocks a step. The
decision about what the strip says lives in iteration_status(), a pure function over plain
facts, so the rules are in one readable place; get_status() only gathers those facts.

A round ends when the researcher records a decision (coding_store/rounds.py, U4). "The
latest run" below means the newest run trained in the open round; a run trained before the
last decision belongs to a closed round.

Signals while the open round has a run, and why each one:
  - Codebook edited since that run (its recorded codebook_version differs from the current
    one): the run doesn't reflect the codebook. A run from before versions were recorded is
    "unknown", never assumed changed.
  - Labels changed since that run: codes or rejections added or removed, "fully coded"
    marks made or withdrawn, "none of these" confirmed or withdrawn. The run didn't learn from
    them. Review decisions write codes and rejections, so a review session ends here too.
  - The run has no review decisions yet: it hasn't been looked at.
  - No decision recorded for the round: evaluate the run and decide what next (Decide becomes
    the current step once the run is reviewed and still up to date).
With no run in the open round, the reason follows from the decision that opened it (refine
the codebook, label more, train again), or "train one" for a scheme's first round. A round
ended by accepting a run shows that round as finished until a new run starts the next.
"""
from . import rounds as rounds_store, runs, schema, themes

STEPS = ("codebook", "train", "review", "decide")

# Where each reason sends the researcher: (top-level tab, sub-tab).
TARGETS = {
    "no_run": ("model", "models"),
    "train_again": ("model", "models"),
    "refine_codebook": ("model", "codebook"),
    "more_labels": ("review", "queues"),
    "accepted": ("model", "runs"),
    "codebook_changed": ("model", "models"),
    "labels_changed": ("model", "models"),
    "not_reviewed": ("review", "queues"),
    "undecided": ("review", "evaluate"),
}


# {kind: (singular, plural)} phrases for describe_label_changes(), in display order.
LABEL_CHANGE_PHRASES = {
    "codes_added": ("code added", "codes added"),
    "codes_removed": ("code removed", "codes removed"),
    "rejections_added": ("rejection added", "rejections added"),
    "rejections_removed": ("rejection removed", "rejections removed"),
    "marks_added": ("interview marked fully coded", "interviews marked fully coded"),
    "marks_removed": ("fully coded mark withdrawn", "fully coded marks withdrawn"),
    "none_added": ('"none of these" confirmed', '"none of these" confirmed'),
    "none_removed": ('"none of these" withdrawn', '"none of these" withdrawn'),
}


def describe_label_changes(changes):
    """'3 codes added, 1 rejection removed' from {kind: count}; empty kinds left out."""
    return ", ".join(
        f"{changes[kind]} {one if changes[kind] == 1 else many}"
        for kind, (one, many) in LABEL_CHANGE_PHRASES.items() if changes.get(kind)
    )


def iteration_status(latest_run, current_codebook_version, label_changes, decisions_on_run, rounds_closed=0,
                     last_decision=None):
    """latest_run: the newest training pass in the open round ({model_version, trained_at,
    engine, codebook_version}) or None. label_changes: {kind: count} since that run, or, with
    no run, since last_decision (see describe_label_changes). decisions_on_run: review
    decisions recorded on the run (not undone). rounds_closed: decisions recorded so far.
    last_decision: the latest of them ({decision, codebook_version, run_ref, created_at}) or None.
    Returns {round, steps: {step: done|current|stale|todo}, reasons: [{code, step, text,
    target: {tab, subpage}}], latest_run, last_decision}, reasons most important first."""
    reasons = []

    def add(code, step, text):
        tab, subpage = TARGETS[code]
        reasons.append({"code": code, "step": step, "text": text, "target": {"tab": tab, "subpage": subpage}})

    steps = {step: "todo" for step in STEPS}
    round_no = rounds_closed + 1
    changed = describe_label_changes(label_changes)
    if latest_run is None:
        kind = last_decision["decision"] if last_decision else None
        if kind is None:
            steps["codebook"] = "done"
            steps["train"] = "current"
            add("no_run", "train", "No model trained for this scheme yet. Train one.")
        elif kind == "accept":
            round_no = rounds_closed  # the round that ended by accepting; a new run starts the next
            steps = {step: "done" for step in STEPS}
            add("accepted", "decide", f"Round {round_no} ended by accepting a run. Export it from Train › Runs; "
                "train again to start a new round.")
        elif kind == "refine_codebook" and last_decision.get("codebook_version") == current_codebook_version:
            steps["codebook"] = "current"
            add("refine_codebook", "codebook",
                f"Round {round_no}: you chose to refine the codebook. Edit it, then retrain.")
        elif kind == "more_labels" and not changed:
            steps["codebook"] = "done"
            add("more_labels", "train", f"Round {round_no}: you chose more labels. Triage a queue, then retrain.")
        else:
            steps["codebook"] = "done"
            steps["train"] = "current"
            why = {"refine_codebook": "the codebook has changed since your decision.",
                   "more_labels": f"since your decision: {changed}.",
                   "another_engine": "you chose to try another engine.",
                   }.get(kind, "you chose to retrain.")
            add("train_again", "train", f"Round {round_no}: {why} Train a new run.")
        return {"round": round_no, "steps": steps, "reasons": reasons, "latest_run": None,
                "last_decision": last_decision}

    steps["codebook"] = steps["train"] = "done"
    recorded = latest_run.get("codebook_version")
    if recorded and recorded != current_codebook_version:
        steps["train"] = "stale"
        add("codebook_changed", "train", "Codebook changed since the latest run. Retrain to use it.")
    if changed:
        steps["train"] = "stale"
        add("labels_changed", "train", f"Since the latest run: {changed}. Retrain to use them.")
    if decisions_on_run:
        steps["review"] = "done"
        if steps["train"] == "done":
            steps["decide"] = "current"
    else:
        steps["review"] = "current" if steps["train"] == "done" else "todo"
        add("not_reviewed", "review", "The latest run hasn't been reviewed yet. Triage a queue.")
    add("undecided", "decide", f"Round {round_no} is open. Evaluate the run and record a decision.")
    return {"round": round_no, "steps": steps, "reasons": reasons, "latest_run": latest_run,
            "last_decision": last_decision}


def label_changes_since(scheme_id, since):
    """{kind: count} of label changes in the scheme after `since` (an ISO timestamp, as every
    timestamp in coding.db). A bulk mark counts by when it was recorded, not the earlier
    moment it's dated as of."""
    theme_ids = [t["theme_id"] for t in themes.list_themes(include_archived=True, scheme_id=scheme_id)]
    out = {}
    with schema.get_conn() as conn:
        for table, added, removed in (("codes", "codes_added", "codes_removed"),
                                      ("negatives", "rejections_added", "rejections_removed")):
            for i in range(0, len(theme_ids), schema.SQLITE_MAX_VARIABLES - 1):
                batch = theme_ids[i:i + schema.SQLITE_MAX_VARIABLES - 1]
                marks = ",".join("?" * len(batch))
                row = conn.execute(
                    f"""SELECT SUM(created_at > ?) AS added, SUM(deleted_at IS NOT NULL AND deleted_at > ?) AS removed
                        FROM {table} WHERE theme_id IN ({marks})""",
                    (since, since, *batch),
                ).fetchone()
                out[added] = out.get(added, 0) + (row["added"] or 0)
                out[removed] = out.get(removed, 0) + (row["removed"] or 0)
        row = conn.execute(
            """SELECT SUM(COALESCE(recorded_at, marked_at) > ?) AS added,
                      SUM(unmarked_at IS NOT NULL AND unmarked_at > ?) AS removed
               FROM item_reviews WHERE scheme_id=?""",
            (since, since, scheme_id),
        ).fetchone()
        out["marks_added"], out["marks_removed"] = row["added"] or 0, row["removed"] or 0
        row = conn.execute(
            """SELECT SUM(confirmed_at > ?) AS added,
                      SUM(unconfirmed_at IS NOT NULL AND unconfirmed_at > ?) AS removed
               FROM segment_none_confirmations WHERE scheme_id=?""",
            (since, since, scheme_id),
        ).fetchone()
        out["none_added"], out["none_removed"] = row["added"] or 0, row["removed"] or 0
    return out


def decisions_on_run(model_version):
    with schema.get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM review_decisions WHERE model_version=? AND undone_at IS NULL",
            (model_version,),
        ).fetchone()["n"]


def get_status(scheme_id, current_codebook_version):
    """iteration_status() for a scheme, from coding.db. The caller passes the current
    codebook version (codebook_export.current_codebook_version()), which keeps this package
    free of a dependency on the export module."""
    decisions = rounds_store.active_decisions(scheme_id)
    last = decisions[-1] if decisions else None
    last_fact = {k: last[k] for k in ("decision", "codebook_version", "run_ref", "created_at", "round_no")} \
        if last else None
    history = runs.list_runs(scheme_id)
    latest = history[0] if history and (last is None or history[0]["trained_at"] > last["created_at"]) else None
    if latest is None:
        changes = label_changes_since(scheme_id, last["created_at"]) if last else {}
        return iteration_status(None, current_codebook_version, changes, 0, len(decisions), last_fact)
    run = {k: latest[k] for k in ("model_version", "trained_at", "engine", "codebook_version")}
    return iteration_status(
        run, current_codebook_version, label_changes_since(scheme_id, latest["trained_at"]),
        decisions_on_run(latest["model_version"]), len(decisions), last_fact,
    )
