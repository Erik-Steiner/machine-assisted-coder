"""Where a scheme stands in the codebook -> train -> review -> decide cycle
(docs/CODEBOOK_PLAN.md U3): the round number, each step's state, and the reasons, most
important first, each naming the tab that resolves it. Backs GET /api/iteration/status and
the one-line cycle strip on the Train and Review pages.

Advisory only, like the codebook's completeness checks: nothing here blocks a step. The
decision about what the strip says lives in iteration_status(), a pure function over plain
facts, so the rules are in one readable place; get_status() only gathers those facts.

Signals, and why each one:
  - No run yet: nothing to review; train first.
  - Codebook edited since the latest run (its recorded codebook_version differs from the
    current one): the run doesn't reflect the codebook. A run from before versions were
    recorded is "unknown", never assumed changed.
  - Labels changed since the latest run: codes or rejections added or removed, "fully coded"
    marks made or withdrawn, "none of these" confirmed or withdrawn. The run didn't learn from
    them. Review decisions write codes and rejections, so a review session ends here too.
  - The latest run has no review decisions yet: it hasn't been looked at.
Rounds close when the researcher records a decision (U4, not built yet), so until then every
scheme is in round 1 and the Decide step is never current.
"""
from . import runs, schema, themes

STEPS = ("codebook", "train", "review", "decide")

# Where each reason sends the researcher: (top-level tab, sub-tab).
TARGETS = {
    "no_run": ("model", "models"),
    "codebook_changed": ("model", "models"),
    "labels_changed": ("model", "models"),
    "not_reviewed": ("review", "queues"),
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


def iteration_status(latest_run, current_codebook_version, label_changes, decisions_on_run, rounds_closed=0):
    """latest_run: the scheme's newest training pass ({model_version, trained_at, engine,
    codebook_version}) or None. label_changes: {kind: count} since that run (see
    describe_label_changes). decisions_on_run: review decisions recorded on it (not undone).
    Returns {round, steps: {step: done|current|stale|todo}, reasons: [{code, step, text,
    target: {tab, subpage}}], latest_run}, reasons most important first."""
    reasons = []

    def add(code, step, text):
        tab, subpage = TARGETS[code]
        reasons.append({"code": code, "step": step, "text": text, "target": {"tab": tab, "subpage": subpage}})

    steps = {step: "todo" for step in STEPS}
    if latest_run is None:
        steps["codebook"] = "done"
        steps["train"] = "current"
        add("no_run", "train", "No model trained for this scheme yet. Train one.")
    else:
        steps["codebook"] = steps["train"] = "done"
        recorded = latest_run.get("codebook_version")
        if recorded and recorded != current_codebook_version:
            steps["train"] = "stale"
            add("codebook_changed", "train", "Codebook changed since the latest run. Retrain to use it.")
        changed = describe_label_changes(label_changes)
        if changed:
            steps["train"] = "stale"
            add("labels_changed", "train", f"Since the latest run: {changed}. Retrain to use them.")
        if decisions_on_run:
            steps["review"] = "done"
        else:
            steps["review"] = "current" if steps["train"] == "done" else "todo"
            add("not_reviewed", "review", "The latest run hasn't been reviewed yet. Triage a queue.")
    return {"round": rounds_closed + 1, "steps": steps, "reasons": reasons, "latest_run": latest_run}


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
    history = runs.list_runs(scheme_id)
    latest = history[0] if history else None
    if latest is None:
        return iteration_status(None, current_codebook_version, {}, 0)
    run = {k: latest[k] for k in ("model_version", "trained_at", "engine", "codebook_version")}
    return iteration_status(
        run, current_codebook_version, label_changes_since(scheme_id, latest["trained_at"]),
        decisions_on_run(latest["model_version"]),
    )
