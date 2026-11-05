"""Rounds of the codebook -> train -> review -> decide cycle (docs/CODEBOOK_PLAN.md U4): the
researcher's decision at the end of each one, and the runs each one contained.

A round is everything between two decisions for one scheme: the codebook as it stood, the
runs trained, the review done on them, and the decision that ended it. Recording a decision
closes the current round and the next one starts with it; round n is closed by the n-th
active decision. This is the provenance a methods section needs ("three rounds; the codebook
changed in round 2 because..."), so decisions are never deleted: withdrawing the latest one
stamps withdrawn_at and reopens its round. Only the latest can be withdrawn, so round numbers
never shift under an earlier decision.

Each decision stores the runs trained during its round (iteration_round_runs), with a stage
column that is NULL for score engines and will name the topic-method stage for Part 2's runs
(U8). Runs and decisions are matched by time: a run belongs to the round that was open when
it was trained.
"""
from . import activity_log, model_runs, runs, schema

# Decision code -> label shown in the UI and the Web Appendix, in display order.
DECISIONS = {
    "refine_codebook": "Refine codebook",
    "more_labels": "More labels",
    "retrain": "Retrain",
    "another_engine": "Try another engine",
    "accept": "Accept this run",
}

# Where the app goes after each decision: (top-level tab, sub-tab). More labels opens Review's
# borderline queue (uncertain), where each decision teaches the next model the most.
NEXT_STEP = {
    "refine_codebook": ("model", "codebook"),
    "more_labels": ("review", "queues"),
    "retrain": ("model", "models"),
    "another_engine": ("model", "models"),
    "accept": ("model", "runs"),
}


def active_decisions(scheme_id):
    """The scheme's decisions that haven't been withdrawn, oldest first (round 1 first)."""
    with schema.get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM iteration_decisions WHERE scheme_id=? AND withdrawn_at IS NULL
               ORDER BY round_no, decision_id""",
            (scheme_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def round_of(trained_at, decisions):
    """The round a run trained at `trained_at` belongs to: 1 + the decisions made before it."""
    return 1 + sum(1 for d in decisions if d["created_at"] < trained_at)


def _round_runs(scheme_id, since, until=None):
    """The scheme's training passes trained after `since` (None = from the start) and, if
    given, up to `until`, oldest first: [{run_ref, engine, trained_at}]."""
    out = []
    for r in runs.list_runs(scheme_id):
        if (since is None or r["trained_at"] > since) and (until is None or r["trained_at"] <= until):
            out.append({"run_ref": r["model_version"], "engine": r["engine"], "trained_at": r["trained_at"]})
    return sorted(out, key=lambda r: r["trained_at"])


def record_decision(scheme_id, decision, run_ref, codebook_version, note=None, coder=schema.DEFAULT_CODER):
    """Close the scheme's current round with `decision` about run `run_ref` (one of the
    scheme's runs). Stores the round's runs with it and logs it. Returns the stored decision
    plus round_no, runs and next ({tab, subpage}). Raises ValueError for an unknown decision
    or a run that didn't train any of the scheme's themes."""
    if decision not in DECISIONS:
        raise ValueError(f"unknown decision '{decision}' -- expected one of {list(DECISIONS)}")
    scheme_runs = {r["model_version"]: r for r in runs.list_runs(scheme_id)}
    if run_ref not in scheme_runs:
        raise ValueError(f"run '{run_ref}' trained none of this scheme's themes")
    previous = active_decisions(scheme_id)
    round_no = len(previous) + 1
    now = schema._now()
    round_runs = _round_runs(scheme_id, previous[-1]["created_at"] if previous else None, now)
    note = (note or "").strip() or None
    with schema.get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO iteration_decisions
                 (scheme_id, round_no, run_ref, codebook_version, decision, note, coder, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (scheme_id, round_no, run_ref, codebook_version, decision, note, coder, now),
        )
        decision_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO iteration_round_runs (decision_id, scheme_id, round_no, run_ref, engine, stage)
               VALUES (?,?,?,?,?,NULL)""",
            [(decision_id, scheme_id, round_no, r["run_ref"], r["engine"]) for r in round_runs],
        )
    run = scheme_runs[run_ref]
    activity_log.log_activity(coder, "iteration_decision", {
        "scheme_id": scheme_id, "round_no": round_no, "decision": decision, "decision_label": DECISIONS[decision],
        "run_ref": run_ref, "engine": model_runs.engine_of_version(run_ref),
        "run_mean_f1": run.get("mean_f1"), "codebook_version": codebook_version, "note": note,
        "round_runs": [r["run_ref"] for r in round_runs],
    })
    tab, subpage = NEXT_STEP[decision]
    return {"decision_id": decision_id, "scheme_id": scheme_id, "round_no": round_no, "decision": decision,
            "run_ref": run_ref, "codebook_version": codebook_version, "note": note, "created_at": now,
            "runs": round_runs, "next": {"tab": tab, "subpage": subpage}}


def withdraw_latest(scheme_id, coder=schema.DEFAULT_CODER):
    """Withdraw the scheme's latest decision, reopening its round. Raises ValueError if there
    is none. Logged."""
    previous = active_decisions(scheme_id)
    if not previous:
        raise ValueError("no decision to withdraw")
    latest = previous[-1]
    with schema.get_conn() as conn:
        conn.execute("UPDATE iteration_decisions SET withdrawn_at=? WHERE decision_id=?",
                     (schema._now(), latest["decision_id"]))
    activity_log.log_activity(coder, "iteration_decision_withdrawn", {
        "scheme_id": scheme_id, "round_no": latest["round_no"], "decision": latest["decision"],
        "decision_label": DECISIONS.get(latest["decision"], latest["decision"]), "run_ref": latest["run_ref"],
    })
    return latest


def list_rounds(scheme_id):
    """{rounds: [{round_no, decision, label, run_ref, codebook_version, note, coder, created_at,
    runs: [{run_ref, engine, stage}]}] oldest first, open: {round_no, runs}} -- the closed
    rounds with what they contained, and the round in progress."""
    decisions = active_decisions(scheme_id)
    with schema.get_conn() as conn:
        run_rows = conn.execute(
            "SELECT decision_id, run_ref, engine, stage FROM iteration_round_runs WHERE scheme_id=?", (scheme_id,),
        ).fetchall()
    by_decision = {}
    for r in run_rows:
        by_decision.setdefault(r["decision_id"], []).append(
            {"run_ref": r["run_ref"], "engine": r["engine"], "stage": r["stage"]})
    closed = [{
        "round_no": d["round_no"], "decision": d["decision"], "label": DECISIONS.get(d["decision"], d["decision"]),
        "run_ref": d["run_ref"], "codebook_version": d["codebook_version"], "note": d["note"], "coder": d["coder"],
        "created_at": d["created_at"],
        "runs": sorted(by_decision.get(d["decision_id"], []), key=lambda r: r["run_ref"]),
    } for d in decisions]
    open_runs = _round_runs(scheme_id, decisions[-1]["created_at"] if decisions else None)
    return {"rounds": closed, "open": {"round_no": len(decisions) + 1, "runs": open_runs}}
