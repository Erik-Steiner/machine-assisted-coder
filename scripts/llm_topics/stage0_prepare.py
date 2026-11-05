"""Stage 0 of the LLM topic-coding pipeline: turn the seed pack and segment scores
into turn-level inputs for discovery (stage 1), the pilot (stage 2) and the full run.
No API calls.

The seed pack and the scores are keyed by *segment* (segmentation.py's merged/split
chunks), but the deliverable is keyed by *turn*. Re-running segmentation's merge and
split rules on the turn exports reproduces every scored segment_id exactly, so the
turn<->segment map below is rebuilt, not guessed -- and is checked against
segmentation.segment_interview() itself, so a future change there fails loudly here.

Labels, per turn and theme (gold_<theme>):
  pos      a segment of this turn was coded with the theme by the researcher
  neg      every segment of this turn was judged, in an interview read in full
           (>= READ_IN_FULL_MIN_CODED coded segments), and none was coded with it
  neutral  everything else. Uncoded segments elsewhere were never examined for the
           theme, so "uncoded" is not evidence of "not the theme".

Scores are used by rank only (pct_<theme>), for stratified sampling and audits --
never shown to the model. Inside the 75 judged interviews, the stored full-fit
`score` was fitted to those interviews' own labels (it memorizes them), so the rank
uses the out-of-fold `oof_score` there instead.

The codebook. The researcher's codebook_seed_themes.json is the source of truth
for definitions, criteria, exemplars and near misses -- this script reads it and
never rewrites it. Only on a first run (no codebook yet) does it write a template:
about 5 script-chosen exemplars per theme, taken from positives-only interviews first
so the read-in-full set stays whole for testing, plus near_miss_candidates.csv for
the researcher to review. Once the near misses are in the codebook, that CSV is
spent; later runs neither read nor rewrite it.

An exemplar is given either by segment ({"segment_id": "447648:0001"}, as the
script's own picks are) or by turn ({"comment_id": "447648_0002"}, the id that
discovery_review.md and the turn exports show) -- see turn_exemplar() for how the
two are checked. Near misses are always turns (near_miss_comment_ids).

Each run checks the codebook against the current export (codebook_resolved.json,
"Codebook issues" in the report): an exemplar no longer coded, or a near miss whose
turn has since been coded with the theme, is kept but marked usable=False for the
researcher to resolve. Every prompt example (exemplar or near miss), and any twin of
it anywhere in the corpus (the same video held once per executive), is flagged
eval_exclude: the model can't be graded on passages it was shown.

The app's codebook export (docs/CODEBOOK_PLAN.md K8). --codebook also takes the
app's codebook_topic_themes.json (codebook_export.py, K7), recognized by its
"format" key, so both kinds of file go to the same flag. Then the app is the source
of truth for everything:
  - the themes: the export's active topics (sub-codes included, each with its
    parent_topic_id); archived topics are left out and listed as codebook issues.
    The seed pack no longer decides which themes there are;
  - definitions, criteria and boundary notes (definition -> description,
    inclusion_criteria -> include, exclusion_criteria -> exclude,
    boundary_notes -> boundary_notes);
  - exemplars and near misses: only the passages the researcher flagged in the app.
    Each is a segment; its turns' comment_ids come from the export's turn_indices
    (comment_id = f"{item_id}_{index + 1:04d}", exactly how export_interview_turns.py
    numbers turns: enumerate(turns, start=1), no turn skipped) and are checked against
    the turn map rebuilt here. A near-miss segment becomes one near-miss entry per
    executive answer turn it touches, since the pipeline's near misses are turns. An
    empty list stays empty -- examples are never picked from model scores for an app
    export -- and is reported as a codebook issue. Twins the app lists
    (twin_segment_ids) are held out of testing along with the text-similarity twins;
  - labels: codebook_labels.csv (next to the JSON, or --labels) replaces the labels
    inferred from segment_topic_scores.csv, which then only supplies rank scores
    (pct_<theme>; empty for a theme the scores don't know). A label row is used when
    its segment is in the turn exports; the others are counted in the report;
  - the gold tier (--read-in-full). `status` (default): interviews the app marked
    read in full (label_policy.read_in_full_counts: confirmed / interactive) -- the
    app's own policy; the labels file already holds their negatives. `coverage` also
    counts interviews flagged inferred_by_coverage (>= 5 coded segments, like the old
    heuristic); every segment of those that has no label for a theme becomes a
    negative here, since the labels file leaves them neutral. That skips the app's
    "judged after the theme was created" rule, which is why it isn't the default.
The codebook's path, format and codebook_version (plus, for an app export, the
scheme, the labels file and its hash, and the gold tier) are recorded in
codebook_resolved.json and the report. A hand-edited codebook_seed_themes.json runs
exactly as before; --labels and --read-in-full apply only to an app export.

Outputs (exports/llm_topics/stage0/, or --out):
  turns_prepared.parquet     one row per turn (all 64,061) with scope, labels, ranks,
                             exemplar_for / near_miss_for / eval_exclude
  codebook_resolved.json     the researcher's codebook plus texts, segment ids and
                             current labels for every example -- what later stages read
  stage0_report.md           counts behind every decision above, and codebook issues
  codebook_seed_themes.json  first run only: the template for the researcher
  near_miss_candidates.csv   first run only: candidates to review

Later stages read exports/llm_topics/stage0/ only; --out is for side runs and checks.

Usage:
    python scripts/llm_topics/stage0_prepare.py                     # uses the researcher's codebook
    python scripts/llm_topics/stage0_prepare.py --codebook other.json
    python scripts/llm_topics/stage0_prepare.py --force             # discards it; script-chosen template
    python scripts/llm_topics/stage0_prepare.py --codebook exports/codebook_topic_themes.json
    python scripts/llm_topics/stage0_prepare.py --codebook exports/codebook_topic_themes.json --read-in-full coverage
"""
import argparse
import hashlib
import json
import re
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd

import common
from segmentation import segment_interview, segment_turn_indices

EXEMPLARS_PER_THEME = 5
EXEMPLAR_WORDS = (30, 220)
NEAR_MISSES_PER_THEME = 8
NEAR_DUP_JACCARD = 0.5

TURN_COLUMNS = [
    "comment_id", "interview_id", "turn_id", "company", "featured_executive",
    "speaker_name", "speaker_role", "question_turn_id", "word_count", "body",
    "turn_timestamp", "interview_title", "publish_date", "channel_name",
    "permalink_comment", "shared_source_url_with",
]

# The app's codebook export (codebook_export.py, K7) and how its fields map onto
# this pipeline's codebook shape (CODEBOOK_PLAN K8).
SEED_FORMAT = "seed_themes"  # the hand-edited codebook_seed_themes.json (no "format" key)
APP_FORMAT = "codebook_topic_themes"
APP_FORMAT_VERSIONS = (1,)
APP_FIELD_MAP = {  # pipeline field -> app export field
    "description": "definition",
    "include": "inclusion_criteria",
    "exclude": "exclusion_criteria",
    "boundary_notes": "boundary_notes",
}
APP_LABELS_NAME = "codebook_labels.csv"
# label_policy.read_in_full_counts, if an export ever lacks it
APP_READ_IN_FULL_STATUSES = ("confirmed", "interactive")
READ_IN_FULL_POLICIES = ("status", "coverage")


class UsageError(Exception):
    """A codebook/option combination that can't run; reported as a usage error."""


def build_turn_segment_map(turns):
    """(interview_id, turn_id, segment_id, segment_text) for every turn piece.

    segmentation.segment_turn_indices() names the turns behind each segment (it
    shares one code path with segment_interview(), so the two can't drift); this
    maps those 0-based positions back to this frame's turn_ids. The texts come
    from segment_interview() itself, and each is checked to be drawn from the
    bodies of the turns it is attributed to, so a wrong pairing fails loudly.
    """
    rows = []
    for interview_id, g in turns.groupby("interview_id", sort=False):
        bodies = g["body"].fillna("").astype(str).tolist()
        speakers = g["speaker_name"].tolist()
        turn_ids = g["turn_id"].tolist()
        record = {
            "item_id": interview_id,
            "turns": [{"content": b, "speaker_name": s} for b, s in zip(bodies, speakers)],
        }
        segments = segment_interview(record)
        indices = segment_turn_indices(record)
        if len(indices) != len(segments):
            raise RuntimeError(f"turn->segment map disagrees with segmentation.py for interview {interview_id}")
        for seg, idx in zip(segments, indices):
            source = " ".join(" ".join(bodies[i] for i in idx).split())
            if not idx or " ".join(seg["text"].split()) not in source:
                raise RuntimeError(f"turn->segment map disagrees with segmentation.py for {seg['segment_id']}")
            rows.extend((interview_id, turn_ids[i], seg["segment_id"], seg["text"]) for i in idx)
    return pd.DataFrame(rows, columns=["interview_id", "turn_id", "segment_id", "segment_text"])


def text_key(text):
    """Collapses the small transcription differences between twin records of one video."""
    return re.sub(r"[^a-z ]", "", str(text).lower())[:120]


def word_set(text):
    return set(re.findall(r"[a-z']+", str(text).lower()))


def near_duplicate(a, b, min_jaccard=NEAR_DUP_JACCARD):
    """Two word sets from the same passage, transcribed differently (twin records,
    re-uploads, a clip of a longer talk). text_key() only catches identical openings."""
    return len(a & b) / max(1, len(a | b)) >= min_jaccard


def segment_labels(scores, themes):
    """Long frame: segment_id x theme_id -> label, rank_score."""
    judged = scores["oof_score"].notna()
    coded_per_item = scores[scores["coded"]].groupby("item_id")["segment_id"].nunique()
    read_in_full = set(coded_per_item[coded_per_item >= common.READ_IN_FULL_MIN_CODED].index)
    positives_only = set(coded_per_item.index) - read_in_full

    lab = scores[["segment_id", "item_id", "theme_id", "coded", "rejected"]].copy()
    lab["label"] = "neutral"
    lab.loc[judged & scores["item_id"].isin(read_in_full), "label"] = "neg"
    lab.loc[scores["rejected"], "label"] = "neg"
    lab.loc[scores["coded"], "label"] = "pos"
    lab["rank_score"] = scores["oof_score"].where(judged, scores["score"])
    return lab[lab["theme_id"].isin(themes)], read_in_full, positives_only


def roll_up_to_turns(tmap, lab, themes):
    """Turn-level gold label and rank score per theme."""
    merged = tmap[["interview_id", "turn_id", "segment_id"]].merge(
        lab[["segment_id", "theme_id", "label", "rank_score"]], on="segment_id", how="inner")
    order = {"neg": 0, "neutral": 1, "pos": 2}
    merged["label_rank"] = merged["label"].map(order)
    # pos if any piece is pos; neg only if every piece is neg; else neutral
    agg = merged.groupby(["interview_id", "turn_id", "theme_id"]).agg(
        max_label=("label_rank", "max"), min_label=("label_rank", "min"), rank_score=("rank_score", "max"))
    agg["gold"] = "neutral"
    agg.loc[agg["min_label"] == 0, "gold"] = "neg"
    agg.loc[agg["max_label"] == 2, "gold"] = "pos"
    wide_gold = agg["gold"].unstack("theme_id").add_prefix("gold_")
    wide_score = agg["rank_score"].unstack("theme_id").add_prefix("score_")
    return wide_gold.join(wide_score).reset_index()


def choose_exemplars(theme_id, seg_pos, read_in_full):
    """Up to EXEMPLARS_PER_THEME positives: answer-only, single-theme, mid-length,
    no near-duplicates, preferring positives-only interviews, then spreading across
    companies and interviews, then most prototypical. Deterministic.

    Prototypical = highest out-of-fold score: the old classifier, never having seen
    this passage, still recognized it as the theme. That favors clear cases over the
    researcher's harder boundary calls, which is what an exemplar is for -- boundary
    cases go in boundary_notes / near misses instead."""
    c = seg_pos[(seg_pos["theme_id"] == theme_id) & seg_pos["answer_only"] & (seg_pos["n_themes"] == 1)
                & seg_pos["words"].between(*EXEMPLAR_WORDS)].copy()
    c = c.drop_duplicates("text_key")
    c["clean"] = c["item_id"].isin(read_in_full)
    chosen, companies, items = [], set(), set()
    while len(chosen) < EXEMPLARS_PER_THEME and len(c):
        c["novelty"] = (~c["company"].isin(companies)).astype(int) * 2 + (~c["item_id"].isin(items)).astype(int)
        pick = c.sort_values(["clean", "novelty", "oof", "segment_id"], ascending=[True, False, False, True]).iloc[0]
        chosen.append(pick)
        companies.add(pick["company"])
        items.add(pick["item_id"])
        c = c[(c["segment_id"] != pick["segment_id"])
              & ~c["words_set"].map(lambda w: near_duplicate(w, pick["words_set"]))]
    return chosen


def file_sha256(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_researcher_codebook(path, themes):
    with open(path, encoding="utf-8") as f:
        cb = json.load(f)
    unknown = [t["topic_id"] for t in cb["topics"] if t["topic_id"] not in themes]
    if unknown:
        raise ValueError(f"{path.name} has topics the segment scores don't know: {unknown}. "
                         f"topic_id must stay the app's theme_id (rename with `name` instead).")
    return cb


def turn_exemplar(e, th, title, turn, turn_segments, issues):
    """An exemplar the researcher gave by turn rather than by segment: the whole turn is
    the example, and its segments are found from the turn map. Labels are checked at
    turn level (gold_<theme>):
      pos      coded with the theme -- usable
      neutral  never judged for the theme. Usable: putting it in the codebook is the
               researcher's judgment. Reported, because the app doesn't know it yet --
               code it there so the TF-IDF model learns from it too
      neg      judged not the theme in a read-in-full interview -- unusable until the
               researcher codes it in the app (a missed positive) or removes it
    A segment-level exemplar that is neutral is instead treated as stale (it was coded
    once), which is why the two forms are checked differently."""
    cids = e.get("comment_ids") or ([e["comment_id"]] if e.get("comment_id") else [])
    if len(cids) != 1:
        issues.append(f"{title}: an exemplar needs a segment_id or exactly one comment_id; got {cids or 'neither'}; skipped")
        return None
    cid = cids[0]
    if cid not in turn.index:
        issues.append(f"{title}: exemplar turn {cid} isn't a turn in the exports; skipped")
        return None
    r = turn.loc[cid]
    gold = r[f"gold_{th}"]
    if r["speaker_role"] != "answer":
        issues.append(f"{title}: exemplar turn {cid} is not an executive answer turn")
    if gold == "neg":
        issues.append(f"{title}: exemplar turn {cid} was judged NOT this theme in a read-in-full interview; "
                      f"marked unusable -- code it in the app if it is the theme, or remove it")
    elif gold == "neutral":
        issues.append(f"{title}: exemplar turn {cid} isn't coded with this theme in the app; used, since the "
                      f"codebook says so -- code it in the app too so the TF-IDF model learns it")
    return {
        "segment_id": None, "segment_ids": list(turn_segments.get(cid, [])), "comment_ids": [cid],
        "company": r["company"], "person": r["speaker_name"], "label_now": gold,
        "usable": gold != "neg", "text": r["body"],
    }


def resolve_codebook(researcher, out, tmap, seg_info, lab, seg_person):
    """Check the researcher's codebook against the current export and attach what
    later stages need (texts, segment ids, current labels). Never edits the
    researcher's file. Returns (topics, issues); issues are human-readable strings.

    An example is kept but marked usable=False when the current labels contradict
    it -- an exemplar that's no longer coded, or a near miss whose turn has since
    been coded with the theme -- so the researcher decides, not this script."""
    label = lab.set_index(["segment_id", "theme_id"])["label"]
    turn = out.set_index("comment_id")
    turn_segments = (tmap.merge(out[["interview_id", "turn_id", "comment_id"]], on=["interview_id", "turn_id"])
                     .groupby("comment_id")["segment_id"].apply(list))
    by_id = {t["topic_id"]: t for t in researcher["topics"]}
    topics, issues = [], []
    for th in lab["theme_id"].unique():
        if th not in by_id:
            issues.append(f"{th}: missing from the codebook; it won't be coded as a seed theme")
    for t in researcher["topics"]:
        th = t["topic_id"]
        title = f"{th} ({t.get('name', th)})"
        for field in ("description", "include", "exclude"):
            if not str(t.get(field) or "").strip():
                issues.append(f"{title}: `{field}` is empty")
        if not t.get("near_miss_comment_ids"):
            issues.append(f"{title}: no near misses")

        exemplars = []
        for e in t.get("exemplars", []):
            sid = e.get("segment_id")
            given_turn = e.get("comment_id") or e.get("comment_ids")
            if sid and sid not in seg_info.index and given_turn:
                # e.g. the app's `dataset_id:item_id` interview label pasted as a segment id
                issues.append(f"{title}: exemplar segment_id `{sid}` isn't a segment; used its turn "
                              f"{given_turn} instead (run lookup.py to see the turn's segments)")
                sid = None
            if not sid:
                # Given by turn: {"comment_id": "447648_0002"} (or "comment_ids": [...]) -- the
                # id discovery_review.md and the turn exports show. See turn_exemplar().
                ex = turn_exemplar(e, th, title, turn, turn_segments, issues)
                if ex:
                    exemplars.append(ex)
                continue
            if sid not in seg_info.index:
                issues.append(f"{title}: exemplar segment {sid} doesn't exist in the turn exports; skipped")
                continue
            info = seg_info.loc[sid]
            now = label.get((sid, th), "unscored")
            if now in ("neg", "neutral"):
                issues.append(f"{title}: exemplar {sid} is not coded with this theme in the current export; "
                              f"marked unusable -- check it")
            if now == "unscored":
                issues.append(f"{title}: exemplar {sid} comes from an interview the current export doesn't score "
                              f"(likely a near-duplicate record); kept, and its twins are held out of testing")
            if not info["answer_only"]:
                issues.append(f"{title}: exemplar {sid} includes non-executive speech")
            exemplars.append({
                "segment_id": sid, "comment_ids": list(info["comment_ids"]), "company": info["company"],
                "person": seg_person.get(sid, e.get("person", "")), "label_now": now,
                "usable": now in ("pos", "unscored"), "text": info["text"],
            })

        near = []
        for cid in t.get("near_miss_comment_ids", []):
            if cid not in turn.index:
                issues.append(f"{title}: near miss {cid} isn't a turn in the exports; skipped")
                continue
            r = turn.loc[cid]
            gold = r[f"gold_{th}"]
            if gold == "pos":
                issues.append(f"{title}: near miss {cid} now has segments coded with this theme, so the turn "
                              f"is a positive; marked unusable -- drop it, or swap in a turn that isn't coded")
            if r["speaker_role"] != "answer":
                issues.append(f"{title}: near miss {cid} is not an executive answer turn")
            near.append({
                "comment_id": cid, "segment_ids": list(turn_segments.get(cid, [])), "company": r["company"],
                "speaker_name": r["speaker_name"], "word_count": int(r["word_count"]), "label_now": gold,
                "usable": gold != "pos", "text": r["body"],
            })

        topics.append({
            "topic_id": th, "name": t.get("name", th), "origin": t.get("origin", "seed"),
            "locked": t.get("locked", True), "description": t.get("description", ""),
            "include": t.get("include", ""), "exclude": t.get("exclude", ""),
            "boundary_notes": t.get("boundary_notes", ""),
            "exemplars": exemplars, "near_misses": near,
        })
    return topics, issues


# ---------------------------------------------------------------- the app's export (K8)

def codebook_format(cb):
    """APP_FORMAT for the app's codebook_topic_themes.json, else SEED_FORMAT (the
    hand-edited codebook_seed_themes.json, which has no "format" key)."""
    if isinstance(cb, dict) and cb.get("format") == APP_FORMAT:
        if cb.get("format_version") not in APP_FORMAT_VERSIONS:
            raise ValueError(f"unsupported {APP_FORMAT} format_version {cb.get('format_version')!r}; "
                             f"this script reads {list(APP_FORMAT_VERSIONS)}")
        return APP_FORMAT
    return SEED_FORMAT


def turn_comment_id(item_id, turn_index):
    """The turn exports' comment_id for a record's 0-based turn index:
    export_interview_turns.py numbers turns with enumerate(turns, start=1) and
    builds f"{item_id}_{i:04d}", skipping none."""
    return f"{item_id}_{int(turn_index) + 1:04d}"


def app_topic_fields(t):
    """A topic's text fields under the pipeline's names (APP_FIELD_MAP); None -> ""."""
    return {ours: str(t.get(theirs) or "") for ours, theirs in APP_FIELD_MAP.items()}


def app_themes(cb):
    """(theme ids coded as seed themes, archived/other topics left out)."""
    active = [t["topic_id"] for t in cb["topics"] if t.get("status", "active") == "active"]
    left_out = [t for t in cb["topics"] if t.get("status", "active") != "active"]
    return active, left_out


def load_app_labels(path):
    labels = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    missing = {"segment_id", "item_id", "theme_id", "label", "label_source", "item_read_in_full_status",
               "inferred_by_coverage"} - set(labels.columns)
    if missing:
        raise ValueError(f"{path.name} lacks columns {sorted(missing)}; is it codebook_export.py's labels CSV?")
    bad = set(labels["label"]) - {"pos", "neg"}
    if bad:
        raise ValueError(f"{path.name}: unexpected label values {sorted(bad)}")
    return labels


def app_segment_labels(labels, scores, themes, tmap, turns, gold_tier, statuses):
    """The app-export counterpart of segment_labels(): the same long frame
    (segment_id x theme_id -> label, rank_score) and gold sets, but the labels are
    codebook_labels.csv's, not inferred from the scores. Returns
    (lab, read_in_full, positives_only, stats); the two sets hold the turn exports'
    own interview_id values."""
    known = set(tmap["segment_id"])
    lb = labels[labels["theme_id"].isin(themes)]
    unmatched = lb[~lb["segment_id"].isin(known)]
    lb = lb[lb["segment_id"].isin(known)].drop_duplicates(["segment_id", "theme_id"])

    per_item = lb.groupby("item_id").agg(status=("item_read_in_full_status", "first"),
                                          coverage=("inferred_by_coverage", lambda v: (v == "true").any()))
    by_status = set(per_item.index[per_item["status"].isin(statuses)])
    gold_items = by_status | (set(per_item.index[per_item["coverage"]]) if gold_tier == "coverage" else set())
    pos_items = set(lb.loc[lb["label"] == "pos", "item_id"])

    labeled = lb[["segment_id", "theme_id", "label"]]
    added = 0
    coverage_only = gold_items - by_status
    if coverage_only:
        # the labels file leaves these interviews' uncoded segments neutral; the
        # coverage tier counts them as judged, like the old >= 5-coded heuristic
        segs = tmap.loc[tmap["interview_id"].astype(str).isin(coverage_only), "segment_id"].drop_duplicates()
        grid = pd.DataFrame([(s, th) for s in segs for th in themes], columns=["segment_id", "theme_id"])
        grid = grid.merge(labeled, on=["segment_id", "theme_id"], how="left")
        extra = grid[grid["label"].isna()].assign(label="neg")
        added = len(extra)
        labeled = pd.concat([labeled, extra], ignore_index=True)

    sc = scores[scores["theme_id"].isin(themes)]
    judged = sc["oof_score"].notna()
    ranks = sc[["segment_id", "theme_id"]].assign(rank_score=sc["oof_score"].where(judged, sc["score"]))
    lab = ranks.drop_duplicates(["segment_id", "theme_id"]).merge(labeled, on=["segment_id", "theme_id"], how="outer")
    lab["label"] = lab["label"].fillna("neutral")

    ids = pd.Series(turns["interview_id"].unique())
    as_ours = lambda items: set(ids[ids.astype(str).isin(items)])
    read_in_full = as_ours(gold_items)
    positives_only = as_ours(pos_items - gold_items)
    stats = {
        "rows_used": len(lb), "by_source": lb.groupby(["label", "label_source"]).size().to_dict(),
        "rows_unmatched": len(unmatched), "items_unmatched": unmatched["item_id"].nunique(),
        "items_by_status": len(by_status), "items_coverage_only": len(coverage_only),
        "coverage_negatives_added": added,
        "labels_other_themes": int((~labels["theme_id"].isin(themes)).sum()),
    }
    return lab, read_in_full, positives_only, stats


def resolve_app_codebook(cb, out, tmap, seg_info, lab, seg_person, themes):
    """resolve_codebook() for the app's export: same checks against current labels,
    same resolved shape (plus parent_topic_id/status and the app's notes and twins).
    Example turn ids come from the export's item_id + turn_indices, checked against
    the turn map rebuilt here; on a mismatch the rebuilt map wins and it's reported."""
    label = lab.set_index(["segment_id", "theme_id"])["label"]
    turn = out.set_index("comment_id")
    turn_segments = (tmap.merge(out[["interview_id", "turn_id", "comment_id"]], on=["interview_id", "turn_id"])
                     .groupby("comment_id")["segment_id"].apply(list))
    topics, issues = [], []
    for t in cb["topics"]:
        th = t["topic_id"]
        title = f"{th} ({t.get('name', th)})"
        if th not in themes:
            issues.append(f"{title}: status `{t.get('status')}` in the app; not coded as a seed theme")
            continue
        fields = app_topic_fields(t)
        for field in ("description", "include", "exclude"):
            if not fields[field].strip():
                issues.append(f"{title}: `{APP_FIELD_MAP[field]}` is empty in the app")
        if not t.get("exemplars"):
            issues.append(f"{title}: no exemplars flagged in the app; the model gets the definition and "
                          f"criteria only (examples are never picked from model scores)")
        if not t.get("near_misses"):
            issues.append(f"{title}: no near misses flagged in the app")

        def example_turns(e, kind):
            sid = e.get("segment_id")
            if sid not in seg_info.index:
                issues.append(f"{title}: {kind} segment {sid} doesn't exist in the turn exports; skipped")
                return sid, None
            rebuilt = list(seg_info.at[sid, "comment_ids"])
            given = [turn_comment_id(e.get("item_id") or seg_info.at[sid, "item_id"], i)
                     for i in e.get("turn_indices") or []]
            if not given:
                issues.append(f"{title}: {kind} {sid} has no turn_indices in the export; "
                              f"used the turn map rebuilt here")
            elif given != rebuilt:
                issues.append(f"{title}: {kind} {sid} turn_indices give {given}, the turn map gives {rebuilt}; "
                              f"used the turn map -- the export and the turn exports may be out of step")
            return sid, rebuilt

        exemplars = []
        for e in t.get("exemplars", []):
            sid, cids = example_turns(e, "exemplar")
            if cids is None:
                continue
            info = seg_info.loc[sid]
            now = label.get((sid, th), "neutral")
            if now != "pos":
                issues.append(f"{title}: exemplar {sid} is not coded with this theme in the labels file; "
                              f"marked unusable -- check it")
            if not info["answer_only"]:
                issues.append(f"{title}: exemplar {sid} includes non-executive speech")
            exemplars.append({
                "segment_id": sid, "comment_ids": cids, "company": info["company"],
                "person": seg_person.get(sid, e.get("person_name", "")), "label_now": now,
                "usable": now == "pos", "text": info["text"],
                "note": e.get("note") or "", "twin_segment_ids": list(e.get("twin_segment_ids") or []),
            })

        near, seen = [], set()
        for nm in t.get("near_misses", []):
            sid, cids = example_turns(nm, "near miss")
            if cids is None:
                continue
            answers = [c for c in cids if c in turn.index and turn.at[c, "speaker_role"] == "answer"]
            if not answers:
                issues.append(f"{title}: near miss {sid} has no executive answer turn; skipped")
                continue
            for cid in answers:
                if cid in seen:
                    continue
                seen.add(cid)
                r = turn.loc[cid]
                gold = r[f"gold_{th}"]
                if gold == "pos":
                    issues.append(f"{title}: near miss {sid} (turn {cid}) is in a turn with segments coded with "
                                  f"this theme, so the turn is a positive; marked unusable -- check it in the app")
                near.append({
                    "comment_id": cid, "segment_ids": list(turn_segments.get(cid, [])), "company": r["company"],
                    "speaker_name": r["speaker_name"], "word_count": int(r["word_count"]), "label_now": gold,
                    "usable": gold != "pos", "text": r["body"],
                    "source_segment_id": sid, "note": nm.get("note") or "",
                    "twin_segment_ids": list(nm.get("twin_segment_ids") or []),
                })

        topics.append({
            "topic_id": th, "name": t.get("name", th), "origin": "seed", "locked": True,
            **fields, "parent_topic_id": t.get("parent_topic_id"), "status": t.get("status", "active"),
            "exemplars": exemplars, "near_misses": near,
        })
    return topics, issues


def auto_codebook(seed, lab, seg_info, seg_person, read_in_full):
    """First run only: script-chosen exemplars, empty criteria, no near misses."""
    # positives are all judged, so their rank_score is the out-of-fold score
    pos = lab[lab["label"] == "pos"][["segment_id", "theme_id", "rank_score"]].rename(columns={"rank_score": "oof"})
    pos = pos.merge(pos.groupby("segment_id").size().rename("n_themes"), on="segment_id")
    seg_pos = pos.join(seg_info, on="segment_id")
    topics = []
    for t in seed:
        picks = choose_exemplars(t["theme_id"], seg_pos, read_in_full)
        topics.append({
            "topic_id": t["theme_id"], "name": t["name"], "origin": "seed", "locked": True,
            "description": t["description"], "include": "", "exclude": "", "boundary_notes": "",
            "exemplars": [{
                "segment_id": p["segment_id"], "comment_ids": p["comment_ids"], "company": p["company"],
                "person": seg_person.get(p["segment_id"], ""), "label_now": "pos", "usable": True,
                "text": p["text"],
            } for p in picks],
            "near_misses": [],
        })
    return topics


def template_topic(t):
    """A resolved topic in the shape the researcher edits."""
    return {
        **{k: t[k] for k in ("topic_id", "name", "origin", "locked", "description", "include", "exclude",
                             "boundary_notes")},
        "near_miss_comment_ids": [nm["comment_id"] for nm in t["near_misses"]],
        "exemplars": [{k: e[k] for k in ("segment_id", "comment_ids", "company", "person", "text")}
                      for e in t["exemplars"]],
    }


def near_miss_candidates(out, themes):
    """Verified negatives the old classifier ranked highest -- first run only. Some
    turn out to be positives the researcher missed, hence researcher_decision:
    near_miss, missed_positive or skip."""
    near = []
    for th in themes:
        c = out[(out[f"gold_{th}"] == "neg") & (out["coded_scope"] == "llm") & (out["word_count"] >= 30)]
        c = c.assign(key=c["body"].map(text_key)).drop_duplicates("key")
        for r in c.nlargest(NEAR_MISSES_PER_THEME, f"score_{th}").itertuples():
            near.append({"topic_id": th, "comment_id": r.comment_id, "company": r.company,
                         "speaker_name": r.speaker_name, "oof_score": round(getattr(r, f"score_{th}"), 3),
                         "researcher_decision": "", "body": r.body})
    return pd.DataFrame(near)


def main(codebook_path, force=False, labels_path=None, gold_tier=None, template_path=None):
    started = datetime.now(timezone.utc)
    template_path = template_path or codebook_path
    app = None  # the app's codebook export, when --codebook is one
    if codebook_path.exists():
        with open(codebook_path, encoding="utf-8") as f:
            if codebook_format(json.load(f)) == APP_FORMAT:
                with open(codebook_path, encoding="utf-8") as g:
                    app = json.load(g)
    if app is None and (labels_path is not None or gold_tier is not None):
        raise UsageError("--labels and --read-in-full apply only to the app's codebook export "
                         f"({APP_FORMAT}); {codebook_path.name} isn't one")
    if app is not None and force:
        raise UsageError("--force rewrites a codebook_seed_themes.json template; it doesn't apply to the app's export")

    seed = common.load_seed_pack()
    if app is None:
        themes = [t["theme_id"] for t in seed]
    else:
        themes, left_out = app_themes(app)
        labels_path = labels_path or codebook_path.with_name(APP_LABELS_NAME)
        if not labels_path.exists():
            raise UsageError(f"{labels_path} not found; export it from the app with the codebook, "
                                    f"or pass --labels")
        labels = load_app_labels(labels_path)
        gold_tier = gold_tier or "status"
        statuses = tuple(app.get("label_policy", {}).get("read_in_full_counts") or APP_READ_IN_FULL_STATUSES)
    turns = common.load_turns()
    scores = pd.read_csv(common.SEGMENT_SCORES)
    print(f"{len(turns):,} turns, {turns['interview_id'].nunique():,} interviews, {len(themes)} seed themes")

    tmap = build_turn_segment_map(turns)
    print(f"turn->segment map: {tmap['segment_id'].nunique():,} segments, verified against segmentation.py")
    missing = set(scores["segment_id"]) - set(tmap["segment_id"])
    if missing:
        raise RuntimeError(f"{len(missing)} scored segments not reproduced from the turn exports")

    label_stats = None
    if app is None:
        lab, read_in_full, positives_only = segment_labels(scores, themes)
    else:
        lab, read_in_full, positives_only, label_stats = app_segment_labels(
            labels, scores, themes, tmap, turns, gold_tier, statuses)
        print(f"  labels from {labels_path.name}: {label_stats['rows_used']:,} rows used, "
              f"{label_stats['rows_unmatched']:,} not in the turn exports; gold tier `{gold_tier}`")

    # --- turn frame: scope, interview flags, gold labels, rank scores
    out = turns[TURN_COLUMNS].copy()
    # one id reads back as a number, several ("123;456") as text
    out["shared_source_url_with"] = out["shared_source_url_with"].fillna("").astype(str).str.replace(r"\.0$", "", regex=True)
    role, words = out["speaker_role"], out["word_count"]
    out["coded_scope"] = "context_only"
    out.loc[role == "other", "coded_scope"] = "excluded_other"
    out.loc[(role == "answer") & (words < common.MIN_CODED_WORDS), "coded_scope"] = "rule_non_substantive"
    out.loc[(role == "answer") & (words >= common.MIN_CODED_WORDS), "coded_scope"] = "llm"
    scored_items = set(scores["item_id"])
    out["interview_scored"] = out["interview_id"].isin(scored_items)
    out["gold_set"] = ""
    out.loc[out["interview_id"].isin(positives_only), "gold_set"] = "positives_only"
    out.loc[out["interview_id"].isin(read_in_full), "gold_set"] = "read_in_full"

    rolled = roll_up_to_turns(tmap, lab, themes)
    out = out.merge(rolled, on=["interview_id", "turn_id"], how="left")
    for th in themes:
        # an app topic with neither labels nor scores yet gets no column from the roll-up
        for col in (f"gold_{th}", f"score_{th}"):
            if col not in out:
                out[col] = float("nan")
        out[f"gold_{th}"] = out[f"gold_{th}"].fillna("neutral")
        in_scope = (out["coded_scope"] == "llm") & out[f"score_{th}"].notna()
        out[f"pct_{th}"] = out.loc[in_scope, f"score_{th}"].rank(pct=True)

    # --- exemplars and near misses
    seg_role = tmap.merge(turns[["interview_id", "turn_id", "speaker_role", "company", "comment_id"]],
                          on=["interview_id", "turn_id"])
    seg_info = seg_role.groupby("segment_id").agg(
        answer_only=("speaker_role", lambda r: bool((r == "answer").all())),
        company=("company", "first"), item_id=("interview_id", "first"),
        text=("segment_text", "first"), comment_ids=("comment_id", list))
    seg_info["words"] = seg_info["text"].str.split().str.len()
    seg_info["text_key"] = seg_info["text"].map(text_key)
    seg_info["words_set"] = seg_info["text"].map(word_set)
    seg_person = scores.drop_duplicates("segment_id").set_index("segment_id")["person_name"]
    model_version = scores["model_version"].iloc[0]

    if app is not None:
        person = labels.drop_duplicates("segment_id").set_index("segment_id")["person_name"]
        seg_person = pd.concat([seg_person, person[~person.index.isin(seg_person.index)]])
        researcher = app
        topics, issues = resolve_app_codebook(app, out, tmap, seg_info, lab, seg_person, themes)
        print(f"  using the app's codebook export {codebook_path.name} ({app.get('codebook_version')}): "
              f"{sum(len(t['exemplars']) for t in topics)} exemplars, "
              f"{sum(len(t['near_misses']) for t in topics)} near-miss turns, {len(issues)} issues")
    elif codebook_path.exists() and not force:
        researcher = load_researcher_codebook(codebook_path, themes)
        topics, issues = resolve_codebook(researcher, out, tmap, seg_info, lab, seg_person)
        print(f"  using researcher codebook {codebook_path.name}: "
              f"{sum(len(t['exemplars']) for t in topics)} exemplars, "
              f"{sum(len(t['near_misses']) for t in topics)} near misses, {len(issues)} issues")
    else:
        topics, issues = auto_codebook(seed, lab, seg_info, seg_person, read_in_full), []
        researcher = None

    # ";"-joined topic ids: one long turn can hold exemplars for several themes
    exemplar_for, near_miss_for = {}, {}
    held_back = []  # word sets of every passage shown to the model as an example
    for t in topics:
        for e in t["exemplars"]:
            for cid in e["comment_ids"]:
                exemplar_for.setdefault(cid, []).append(t["topic_id"])
            # a turn-level exemplar (turn_exemplar) has no single segment_id, only segment_ids
            held_back.extend(seg_info.loc[e.get("segment_ids") or [e["segment_id"]], "words_set"])
        for nm in t["near_misses"]:
            near_miss_for.setdefault(nm["comment_id"], []).append(t["topic_id"])
            held_back.extend(seg_info.loc[nm["segment_ids"], "words_set"])
    out["exemplar_for"] = out["comment_id"].map(lambda c: ";".join(exemplar_for.get(c, [])))
    out["near_miss_for"] = out["comment_id"].map(lambda c: ";".join(near_miss_for.get(c, [])))

    # Prompt examples, and their twins anywhere in the corpus, can't be test items:
    # the model would be graded on passages it was shown.
    is_twin = seg_info["words_set"].map(lambda w: any(near_duplicate(w, e) for e in held_back))
    # plus the twins the app itself lists for an example (app export only; empty otherwise)
    listed_twins = {s for t in topics for e in t["exemplars"] + t["near_misses"] for s in e.get("twin_segment_ids", [])}
    if listed_twins:
        is_twin = is_twin | seg_info.index.isin(listed_twins)
    twin_turns = tmap[tmap["segment_id"].isin(set(seg_info.index[is_twin]))][["interview_id", "turn_id"]]
    out["eval_exclude"] = pd.MultiIndex.from_frame(out[["interview_id", "turn_id"]]).isin(
        pd.MultiIndex.from_frame(twin_turns))
    out.loc[(out["exemplar_for"] != "") | (out["near_miss_for"] != ""), "eval_exclude"] = True

    # --- write
    common.STAGE0_DIR.mkdir(parents=True, exist_ok=True)
    out.to_parquet(common.STAGE0_DIR / "turns_prepared.parquet", index=False)
    if researcher is None:
        # First run: hand the researcher a template to edit, plus near-miss candidates.
        codebook_path = template_path
        common.write_json(codebook_path, {
            "codebook_version": "seed-0",
            "created_at": started.isoformat(timespec="seconds"),
            "source_model_version": model_version,
            "topics": [template_topic(t) for t in topics],
        })
        near_miss_candidates(out, themes).to_csv(
            common.STAGE0_DIR / "near_miss_candidates.csv", index=False, encoding="utf-8-sig")

    # Which codebook this run used, so later stages and the researcher can tell.
    source = {
        "codebook_format": APP_FORMAT if app is not None else SEED_FORMAT,
        "codebook_path": str(codebook_path.resolve()),
        "codebook_version": (researcher or {}).get("codebook_version", "seed-0"),
    }
    if app is not None:
        source.update({
            "codebook_format_version": app.get("format_version"),
            "codebook_generated_at": app.get("generated_at"),
            "scheme": app.get("scheme"),
            "labels_path": str(labels_path.resolve()),
            "labels_sha256": file_sha256(labels_path),
            "gold_tier": gold_tier,
            "read_in_full_statuses": list(statuses),
            "topics_left_out": [{"topic_id": t["topic_id"], "status": t.get("status")} for t in left_out],
        })
    else:
        source["gold_tier"] = f"coded_segments>={common.READ_IN_FULL_MIN_CODED}"

    common.write_json(common.STAGE0_DIR / "codebook_resolved.json", {
        "codebook_source": codebook_path.name,
        "codebook_sha256": file_sha256(codebook_path),
        "resolved_at": started.isoformat(timespec="seconds"),
        "source_model_version": model_version,
        **source,
        "issues": issues,
        "topics": topics,
    })
    write_report(out, themes, read_in_full, positives_only, tmap, scores, topics, issues, researcher is not None,
                 source, label_stats)
    print(f"wrote {common.STAGE0_DIR}")


def write_report(out, themes, read_in_full, positives_only, tmap, scores, topics, issues, from_researcher,
                 source=None, label_stats=None):
    llm = out["coded_scope"] == "llm"
    app = source is not None and source["codebook_format"] == APP_FORMAT
    lines = [
        "# Stage 0 report", "",
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by scripts/llm_topics/stage0_prepare.py. "
        f"Segment scores from model `{scores['model_version'].iloc[0]}`.", "",
    ]
    if source is not None:
        lines += [
            "## Codebook source", "",
            f"- Codebook: `{source['codebook_path']}` (format `{source['codebook_format']}`, "
            f"version `{source['codebook_version']}`).",
        ]
        if app:
            scheme = source.get("scheme") or {}
            lines += [
                f"- Scheme: `{scheme.get('scheme_id')}` ({scheme.get('name')}); exported "
                f"{source.get('codebook_generated_at')}.",
                f"- Labels: `{source['labels_path']}` ({source['labels_sha256']}), used instead of labels "
                f"inferred from the segment scores; the scores only rank turns.",
                f"- Gold tier `{source['gold_tier']}`: read in full = status in "
                f"{'/'.join(source['read_in_full_statuses'])}"
                + (" or inferred_by_coverage." if source["gold_tier"] == "coverage" else "."),
                f"- Topics left out (not active): "
                f"{', '.join(f'{t['topic_id']} ({t['status']})' for t in source['topics_left_out']) or 'none'}.",
            ]
        lines.append("")
    lines += [
        "## Scope", "",
        "| coded_scope | turns | words |", "|---|---|---|",
    ]
    for scope, g in out.groupby("coded_scope"):
        lines.append(f"| {scope} | {len(g):,} | {int(g['word_count'].sum()):,} |")
    lines += [
        "", f"- Interviews: {out['interview_id'].nunique():,}; never scored by the TF-IDF model: "
        f"{(~out.drop_duplicates('interview_id')['interview_scored']).sum()} "
        f"({(llm & ~out['interview_scored']).sum():,} llm-scope turns with no rank score).",
        f"- Turns split across several segments: {(tmap.groupby(['interview_id','turn_id']).size() > 1).sum():,}; "
        f"segments spanning several turns: {(tmap.groupby('segment_id').size() > 1).sum():,}.",
        "", "## Gold sets", "",
    ]
    rule = (f"read in full in the app, tier `{source['gold_tier']}`" if app
            else f">= {common.READ_IN_FULL_MIN_CODED} coded segments")
    lines += [
        f"- read_in_full: {len(read_in_full)} interviews ({rule}): "
        f"{out[out['gold_set']=='read_in_full'].drop_duplicates('interview_id')['company'].value_counts().to_dict()}",
        f"- positives_only: {len(positives_only)} interviews; their uncoded segments are neutral.",
    ]
    if label_stats is not None:
        s = label_stats
        lines += [
            f"- Labels file rows used: {s['rows_used']:,} "
            f"({', '.join(f'{lab} {src}: {n:,}' for (lab, src), n in sorted(s['by_source'].items()))}); "
            f"not in the turn exports, ignored: {s['rows_unmatched']:,} rows from {s['items_unmatched']} "
            f"interviews; for topics left out: {s['labels_other_themes']:,}.",
            f"- Read-in-full interviews by app status: {s['items_by_status']}; added by coverage only: "
            f"{s['items_coverage_only']} ({s['coverage_negatives_added']:,} uncoded segment x theme pairs "
            f"counted as negatives).",
        ]
    lines += [
        "", "| theme | name | pos turns | neg turns | pos in read_in_full | exemplars | near misses "
        "| held-out pos (llm scope) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    by_id = {t["topic_id"]: t for t in topics}
    for th in themes:
        t = by_id.get(th, {"name": "(not in codebook)", "exemplars": [], "near_misses": []})
        g = out[f"gold_{th}"]
        held = ((g == "pos") & llm & ~out["eval_exclude"]).sum()
        usable = lambda items: f"{sum(i['usable'] for i in items)}/{len(items)}"
        name = t["name"] + (f" (sub-code of {t['parent_topic_id']})" if t.get("parent_topic_id") else "")
        lines.append(f"| {th} | {name} | {(g=='pos').sum()} | {(g=='neg').sum():,} | "
                     f"{((g=='pos') & (out['gold_set']=='read_in_full')).sum()} | {usable(t['exemplars'])} | "
                     f"{usable(t['near_misses'])} | {held} |")
    lines += [
        "", "Exemplar and near-miss counts are usable/listed (see Codebook issues).",
        f"- eval_exclude turns (prompt examples and their twins): {int(out['eval_exclude'].sum())}.",
        "- Positives outside llm scope (interviewer/narrator turns) are kept as labels but won't be coded.",
    ]
    if from_researcher:
        lines += ["", "## Codebook issues", ""] + ([f"- {i}" for i in issues] or ["None."])
    else:
        lines += [
            "", "## Researcher to-do before stage 1", "",
            "1. Fill `include`, `exclude` and `boundary_notes` for each theme in `codebook_seed_themes.json`.",
            "2. Check the chosen exemplars; swap any that are weak or atypical.",
            "3. In `near_miss_candidates.csv`, mark each row's `researcher_decision`: `near_miss`, "
            "`missed_positive` (it is the theme -- the gold label gets corrected) or `skip`. "
            "Copy 2-3 `near_miss` comment_ids per theme into `near_miss_comment_ids`.",
        ]
    (common.STAGE0_DIR / "stage0_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--codebook", type=Path, default=common.STAGE0_DIR / "codebook_seed_themes.json",
                        help="the researcher's codebook (default: stage0/codebook_seed_themes.json), or the app's "
                             f"codebook export ({APP_FORMAT}.json, recognized by its \"format\" key)")
    parser.add_argument("--labels", type=Path, default=None,
                        help=f"app export only: the labels CSV (default: {APP_LABELS_NAME} next to --codebook)")
    parser.add_argument("--read-in-full", choices=READ_IN_FULL_POLICIES, default=None,
                        help="app export only: which interviews are the gold tier. status (default): marked read in "
                             "full in the app (confirmed/interactive); coverage: also those inferred_by_coverage")
    parser.add_argument("--out", type=Path, default=None,
                        help="write stage 0's outputs here instead of exports/llm_topics/stage0/ (side runs and "
                             "checks; later stages read the default folder only)")
    parser.add_argument("--force", action="store_true",
                        help="ignore the researcher's codebook and overwrite it, and near_miss_candidates.csv, "
                             "with fresh script-chosen ones (discards the researcher's edits)")
    args = parser.parse_args()
    template = None
    if args.out is not None:
        common.STAGE0_DIR = args.out
        # a first-run template goes to --out too, never into exports/
        template = args.out / "codebook_seed_themes.json"
    try:
        main(args.codebook, force=args.force, labels_path=args.labels, gold_tier=args.read_in_full,
             template_path=template)
    except UsageError as e:
        parser.error(str(e))
