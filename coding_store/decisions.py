"""Triage decisions from the Review tab's queues (roadmap B2): accept / reject / skip
on one passage for one theme, recorded with the queue context they were made in
(mode, engine, model_version, the model's score), plus undo.

Why a table of its own, not just codes/negatives rows: rejections and skips aren't
codes, and the per-round statistics roadmap C2 builds -- accept rate by queue mode,
precision above the threshold estimated from the random-check ("audit") queue --
need every passage shown, including skips. A skipped audit passage that silently
vanished would bias that estimate. This is the documented exception to CLAUDE.md's
"tag codes rather than add an audit table" convention.

What each decision does to the labels (same in every queue mode):
  accept  "this passage IS the theme" -> codes.add_code(source="review_accept"),
          which also cancels the coder's rejection (codes' exclusivity rule).
  reject  "this passage is NOT the theme" -> codes.add_negative(source="review_reject"),
          which also cancels the coder's code.
  skip    nothing changes; the decision is still recorded.
An accept can also flag the passage as an exemplar, and a reject as a near miss
(`flag`; coding_store/examples.py). The decision itself stays accept/reject, so
round counts don't change; prev_flag/prev_flag_note keep what the row had before.
prev_state records the passage's state for that theme and coder just before
("coded" | "rejected" | "none"), so undo() can put back exactly what was there --
reactivating the original code/rejection row (keeping its original created_at/
source/note) rather than writing a new one.

A "round" is one theme's queue for one run: round_id = model_version:theme_id:mode.
Depends on schema, codes and examples.
"""
from . import codes, examples, schema

FLAGS = {"exemplar": "accept", "near_miss": "reject"}  # flag -> the decision it rides on

DECISIONS = ("accept", "reject", "skip")


def _state(conn, segment_id, theme_id, coder):
    if conn.execute(
        "SELECT 1 FROM codes WHERE segment_id=? AND theme_id=? AND coder=? AND deleted_at IS NULL",
        (segment_id, theme_id, coder),
    ).fetchone():
        return "coded"
    if conn.execute(
        "SELECT 1 FROM negatives WHERE segment_id=? AND theme_id=? AND coder=? AND deleted_at IS NULL",
        (segment_id, theme_id, coder),
    ).fetchone():
        return "rejected"
    return "none"


def round_id(model_version, queue_theme_id, mode):
    return f"{model_version}:{queue_theme_id}:{mode}"


def _row_flag(conn, state, segment_id, theme_id, coder):
    """(flag, note) of the coder's active row behind `state` ("coded"/"rejected"), or (None, None)."""
    kind = {"coded": "exemplar", "rejected": "near_miss"}.get(state)
    if kind is None:
        return None, None
    table, id_col, flag_col, note_col = examples.KINDS[kind]
    row = conn.execute(
        f"""SELECT {flag_col} AS f, {note_col} AS n FROM {table} WHERE segment_id=? AND theme_id=? AND coder=?
            AND deleted_at IS NULL ORDER BY {id_col} DESC LIMIT 1""",
        (segment_id, theme_id, coder),
    ).fetchone()
    return (row["f"], row["n"]) if row else (None, None)


def record_decision(segment_id, theme_id, decision, *, mode, engine, model_version, queue_theme_id,
                    score=None, oof_score=None, coder=schema.DEFAULT_CODER, flag=None):
    """Apply and record one triage decision. theme_id is the theme being decided
    (in the overlap queue that's the *other* theme; queue_theme_id is the queue's
    own theme, which names the round). Returns the stored decision row."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    if flag is not None and FLAGS.get(flag) != decision:
        raise ValueError("an exemplar flag goes with accept, a near-miss flag with reject")
    with schema.get_conn() as conn:
        prev_state = _state(conn, segment_id, theme_id, coder)
        prev_flag, prev_flag_note = _row_flag(conn, prev_state, segment_id, theme_id, coder)
    note = f"review queue: {mode}"
    if decision == "accept":
        codes.add_code(segment_id, theme_id, coder=coder, source="review_accept", note=note)
    elif decision == "reject":
        codes.add_negative(segment_id, theme_id, coder=coder, source="review_reject", note=note)
    if flag:
        examples.set_flag(segment_id, theme_id, flag, True, coder=coder)
    with schema.get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO review_decisions
                 (round_id, segment_id, theme_id, queue_theme_id, mode, engine, model_version,
                  score, oof_score, decision, prev_state, flag, prev_flag, prev_flag_note, coder, decided_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (round_id(model_version, queue_theme_id, mode), segment_id, theme_id, queue_theme_id,
             mode, engine, model_version, score, oof_score, decision, prev_state, flag, prev_flag,
             prev_flag_note, coder, schema._now()),
        )
        row = conn.execute("SELECT * FROM review_decisions WHERE decision_id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def _reactivate_latest(conn, kind, segment_id, theme_id, coder, prev_flag, prev_flag_note):
    """Clear deleted_at on the most recent row for (segment, theme, coder) -- the one
    the decision soft-deleted (or flagged) -- so its original provenance comes back
    intact, and put its exemplar / near-miss flag and note back as they were."""
    table, id_col, flag_col, note_col = examples.KINDS[kind]
    latest = conn.execute(
        f"SELECT {id_col} FROM {table} WHERE segment_id=? AND theme_id=? AND coder=? ORDER BY {id_col} DESC LIMIT 1",
        (segment_id, theme_id, coder),
    ).fetchone()
    if latest:
        conn.execute(f"UPDATE {table} SET deleted_at=NULL, {flag_col}=?, {note_col}=? WHERE {id_col}=?",
                     (prev_flag or 0, prev_flag_note, latest[id_col]))


def undo(decision_id, coder=schema.DEFAULT_CODER):
    """Undo a decision: restore the passage's prev_state for that theme and mark the
    decision undone (never deleted). Only the most recent active decision on that
    passage+theme can be undone -- an older one's prev_state no longer describes
    what's there. Raises ValueError otherwise. Returns the updated decision row."""
    now = schema._now()
    with schema.get_conn() as conn:
        d = conn.execute("SELECT * FROM review_decisions WHERE decision_id=?", (decision_id,)).fetchone()
        if d is None:
            raise ValueError(f"unknown decision {decision_id}")
        if d["undone_at"]:
            raise ValueError("that decision was already undone")
        newer = conn.execute(
            """SELECT 1 FROM review_decisions WHERE segment_id=? AND theme_id=? AND coder=?
               AND undone_at IS NULL AND decision_id > ?""",
            (d["segment_id"], d["theme_id"], d["coder"], decision_id),
        ).fetchone()
        if newer:
            raise ValueError("a later decision on this passage and theme exists -- undo that one first")
        seg, theme, who = d["segment_id"], d["theme_id"], d["coder"]
        if d["decision"] != "skip":
            # Drop whatever the decision made active, then bring back what was there.
            conn.execute("UPDATE codes SET deleted_at=? WHERE segment_id=? AND theme_id=? AND coder=? "
                         "AND deleted_at IS NULL", (now, seg, theme, who))
            conn.execute("UPDATE negatives SET deleted_at=? WHERE segment_id=? AND theme_id=? AND coder=? "
                         "AND deleted_at IS NULL", (now, seg, theme, who))
            if d["prev_state"] == "coded":
                _reactivate_latest(conn, "exemplar", seg, theme, who, d["prev_flag"], d["prev_flag_note"])
            elif d["prev_state"] == "rejected":
                _reactivate_latest(conn, "near_miss", seg, theme, who, d["prev_flag"], d["prev_flag_note"])
        conn.execute("UPDATE review_decisions SET undone_at=? WHERE decision_id=?", (now, decision_id))
        row = conn.execute("SELECT * FROM review_decisions WHERE decision_id=?", (decision_id,)).fetchone()
    return dict(row)


def round_summary(model_version, queue_theme_id, mode):
    """Counts of active (not undone) decisions in one round: {accept, reject, skip, total}."""
    with schema.get_conn() as conn:
        rows = conn.execute(
            """SELECT decision, COUNT(*) AS n FROM review_decisions
               WHERE round_id=? AND undone_at IS NULL GROUP BY decision""",
            (round_id(model_version, queue_theme_id, mode),),
        ).fetchall()
    out = {d: 0 for d in DECISIONS}
    out.update({r["decision"]: r["n"] for r in rows})
    out["total"] = sum(out[d] for d in DECISIONS)
    return out
