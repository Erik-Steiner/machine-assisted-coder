"""Exemplars and near misses (docs/CODEBOOK_PLAN.md K3): the passages a researcher
stands behind as showing a theme, and the ones that look like it but aren't.

  exemplar   a coded passage flagged as a clear, typical case: codes.is_exemplar on
             the coder's *active* code row (+ optional exemplar_note: why it's typical).
  near miss  a rejected passage flagged as a useful contrast: negatives.is_near_miss on
             the coder's *active* rejection row (+ near_miss_note: what makes it not the theme).

Flags live on the code / rejection row itself, so a flag can't exist without the
decision behind it, and removing that decision (a soft delete) takes the flag with it
(see codes.add_code()/add_negative(), which also start a re-applied row unflagged).
These are what the codebook export (K7) hands to a model as examples -- never the
model's own top predictions.

Twins: the same passage often exists twice (one video held as two records, re-uploads,
a clip of a longer talk). Flagging both would show a model the same example twice, so
set_flag() warns -- never blocks -- when an already-flagged passage of the same kind
for the theme looks like a twin: word-set Jaccard >= TWIN_JACCARD, and the same speaker
(person_name) or items in the same near-duplicate cluster (scripts/find_duplicates.py).
Same rule as scripts/llm_topics/stage0_prepare.py's near_duplicate().

Depends on schema and duplicates.
"""
import re

from . import duplicates, schema

TWIN_JACCARD = 0.5
KINDS = {
    # kind: (table, id column, flag column, note column)
    "exemplar": ("codes", "code_id", "is_exemplar", "exemplar_note"),
    "near_miss": ("negatives", "negative_id", "is_near_miss", "near_miss_note"),
}


def word_set(text):
    return set(re.findall(r"[a-z']+", (text or "").lower()))


def jaccard(a, b):
    return len(a & b) / max(1, len(a | b))


def _active_row(conn, kind, segment_id, theme_id, coder):
    table, id_col, _, _ = KINDS[kind]
    return conn.execute(
        f"""SELECT * FROM {table} WHERE segment_id=? AND theme_id=? AND coder=? AND deleted_at IS NULL
            ORDER BY {id_col} DESC LIMIT 1""",
        (segment_id, theme_id, coder),
    ).fetchone()


def list_flagged(theme_id, kind):
    """Active flagged rows of `kind` for a theme, joined with segment text/metadata,
    oldest flag first: [{segment_id, item_id, dataset_id, group_name, person_name,
    speaker_name, speaker_role, text, note, coder, created_at}]."""
    table, id_col, flag_col, note_col = KINDS[kind]
    with schema.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT r.segment_id, r.coder, r.created_at, r.{note_col} AS note,
                       s.item_id, s.dataset_id, s.group_name, s.person_name, s.speaker_name,
                       s.speaker_role, s.text
                FROM {table} r JOIN segments s ON s.segment_id = r.segment_id
                WHERE r.theme_id=? AND r.deleted_at IS NULL AND r.{flag_col}=1
                ORDER BY r.{id_col}""",
            (theme_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["item_id"] = str(d["item_id"])
        out.append(d)
    return out


def _cluster_of(conn, run_id, dataset_id, item_id):
    if not run_id:
        return None
    row = conn.execute(
        "SELECT cluster_id FROM duplicate_cluster_items WHERE run_id=? AND dataset_id=? AND item_id=?",
        (run_id, dataset_id, str(item_id)),
    ).fetchone()
    return row["cluster_id"] if row else None


def find_twins(segment_id, theme_id, kind):
    """Already-flagged passages of `kind` for the theme that look like twins of
    segment_id (see the module docstring). [{segment_id, similarity, text}]."""
    with schema.get_conn() as conn:
        seg = conn.execute("SELECT * FROM segments WHERE segment_id=?", (segment_id,)).fetchone()
        if seg is None:
            return []
        run_id = duplicates.get_latest_duplicate_run_id()
        own_cluster = _cluster_of(conn, run_id, seg["dataset_id"], seg["item_id"])
        words = word_set(seg["text"])
        twins = []
        for other in list_flagged(theme_id, kind):
            if other["segment_id"] == segment_id:
                continue
            same_speaker = bool(seg["person_name"]) and other["person_name"] == seg["person_name"]
            same_cluster = own_cluster is not None and \
                _cluster_of(conn, run_id, other["dataset_id"], other["item_id"]) == own_cluster
            if not (same_speaker or same_cluster):
                continue
            sim = jaccard(words, word_set(other["text"]))
            if sim >= TWIN_JACCARD:
                twins.append({"segment_id": other["segment_id"], "similarity": round(sim, 2),
                              "text": other["text"][:200]})
    return twins


def set_flag(segment_id, theme_id, kind, flagged, note=None, coder=schema.DEFAULT_CODER):
    """Flag (or unflag) the coder's active code (kind="exemplar") or rejection
    (kind="near_miss") on segment_id for theme_id. Flagging an already-flagged row
    just updates its note. Raises ValueError when there's no active row to flag --
    an exemplar has to be coded, a near miss rejected. Returns {flagged, twins}:
    twins are warnings only (see find_twins()); the flag is set regardless."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    table, id_col, flag_col, note_col = KINDS[kind]
    with schema.get_conn() as conn:
        row = _active_row(conn, kind, segment_id, theme_id, coder)
        if row is None:
            need = "coded with" if kind == "exemplar" else "rejected for"
            raise ValueError(f"this passage isn't {need} that theme, so it can't be flagged as a "
                             f"{'exemplar' if kind == 'exemplar' else 'near miss'}")
        if flagged:
            conn.execute(f"UPDATE {table} SET {flag_col}=1, {note_col}=? WHERE {id_col}=?",
                         ((note or "").strip() or None, row[id_col]))
        else:
            conn.execute(f"UPDATE {table} SET {flag_col}=0, {note_col}=NULL WHERE {id_col}=?", (row[id_col],))
    return {"flagged": bool(flagged), "twins": find_twins(segment_id, theme_id, kind) if flagged else []}


def annotate_twins(rows):
    """Adds twin_of: [segment_id, ...] to each row of a list_flagged() result -- the
    other rows in the same list that look like twins of it (same rule as find_twins()),
    so the Codebook page can show where two flags are really one passage."""
    with schema.get_conn() as conn:
        run_id = duplicates.get_latest_duplicate_run_id()
        clusters = [_cluster_of(conn, run_id, r["dataset_id"], r["item_id"]) for r in rows]
    words = [word_set(r["text"]) for r in rows]
    for r in rows:
        r["twin_of"] = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            linked = (rows[i]["person_name"] and rows[i]["person_name"] == rows[j]["person_name"]) or \
                (clusters[i] is not None and clusters[i] == clusters[j])
            if linked and jaccard(words[i], words[j]) >= TWIN_JACCARD:
                rows[i]["twin_of"].append(rows[j]["segment_id"])
                rows[j]["twin_of"].append(rows[i]["segment_id"])
    return rows


def flagged_theme_ids(segment_ids, kind, coder=schema.DEFAULT_CODER):
    """{segment_id: [theme_id, ...]} where the coder's active row of `kind` is flagged --
    for drawing stars on chips."""
    if not segment_ids:
        return {}
    table, _, flag_col, _ = KINDS[kind]
    out = {}
    batch_size = schema.SQLITE_MAX_VARIABLES - 1
    with schema.get_conn() as conn:
        for i in range(0, len(segment_ids), batch_size):
            batch = segment_ids[i:i + batch_size]
            placeholders = ",".join("?" * len(batch))
            for r in conn.execute(
                f"""SELECT segment_id, theme_id FROM {table}
                    WHERE coder=? AND deleted_at IS NULL AND {flag_col}=1 AND segment_id IN ({placeholders})""",
                (coder, *batch),
            ):
                out.setdefault(r["segment_id"], []).append(r["theme_id"])
    return out
