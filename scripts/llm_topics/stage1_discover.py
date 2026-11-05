"""Stage 1 of the LLM topic-coding pipeline: discover emergent topics around the
researcher's five locked themes. Produces a draft for the researcher to edit --
nothing here codes the corpus.

Why emergent topics at all: the five seed themes are theory-driven and cover only a
few percent of executive talk. Without topics for the rest (products, research,
compute, policy...), most turns would land in "other" and the topic summary would say
little. The locked themes stay exactly as the researcher defined them; discovery may
only add topics *beside* them, never redefine or duplicate them.

Method (map -> reduce, both on DISCOVERY_MODEL):
  sample  ~1,500 executive answer turns (coded_scope == "llm", >= 20 words), never from
          read-in-full interviews (they are the test set) and never a prompt example.
          ~70% random, stratified by company with a floor so Google and DeepMind are
          visible; ~30% from turns the old classifier ranked in the top decile for some
          seed theme, so the model sees where the locked themes' boundaries are.
  map     batches of ~100 turns; each call proposes candidate topics for what the
          locked themes don't cover, with example turn ids.
  reduce  one call merges every batch's candidates into TARGET_TOPICS emergent topics
          with definitions, include/exclude criteria, boundary notes and examples.

Inputs:  stage0/turns_prepared.parquet, stage0/codebook_resolved.json
Outputs (exports/llm_topics/stage1/):
  sample.parquet              the sampled turns, their stratum and batch
  raw/                        one record per API call (llm.py) -- provenance, and resume
  candidates.json             every map call's candidate topics
  emergent_topics_proposed.json  the model's proposal, rewritten on every run
  emergent_topics_draft.json  the researcher's working copy -- written once from the proposal,
                              never overwritten after (same shape as the
                              seed codebook; origin "emergent", locked false)
  discovery_review.md         the draft in readable form, with example texts

Usage:
    python scripts/llm_topics/stage1_discover.py          # dry run: sample, prompts, cost estimate
    python scripts/llm_topics/stage1_discover.py --run    # calls the API (resumes if interrupted)
"""
import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd
from pydantic import BaseModel

import common

SAMPLE_SIZE = 1500
SEEDED_SHARE = 0.30          # share of the sample drawn from top-decile seed-theme turns
COMPANY_FLOOR = 200          # minimum random-stratum turns per company
MIN_WORDS = 20
BATCH_SIZE = 100
MAX_TURN_WORDS = 300         # longer answers are cut for discovery (topic, not detail, matters here)
MAX_QUESTION_WORDS = 60
TARGET_TOPICS = "10-20"
SEED = 20261007
WORKERS = 4

# Sonnet 5.5 list prices, $/million tokens; only for the dry-run estimate.
PRICE_IN, PRICE_OUT, PRICE_CACHE_READ = 2.00, 10.00, 0.20


# ---------------------------------------------------------------- output schemas

class CandidateTopic(BaseModel):
    name: str
    definition: str
    include: str
    exclude: str
    borders_locked_themes: list[str]
    example_comment_ids: list[str]
    approx_turns_in_batch: int


class LockedThemeCount(BaseModel):
    topic_id: str
    approx_turns_in_batch: int


class MapResult(BaseModel):
    candidate_topics: list[CandidateTopic]
    locked_theme_counts: list[LockedThemeCount]
    approx_non_substantive_turns: int
    notes: str


class EmergentTopic(BaseModel):
    topic_id: str
    name: str
    definition: str
    include: str
    exclude: str
    boundary_notes: str
    example_comment_ids: list[str]
    merged_from: list[str]
    approx_sample_turns: int


class DroppedCandidate(BaseModel):
    name: str
    reason: str


class ReduceResult(BaseModel):
    emergent_topics: list[EmergentTopic]
    dropped_candidates: list[DroppedCandidate]
    notes: str


# ---------------------------------------------------------------- sampling

def draw_sample(turns, themes, n=SAMPLE_SIZE, seed=SEED):
    pool = turns[(turns["coded_scope"] == "llm") & (turns["word_count"] >= MIN_WORDS)
                 & (turns["gold_set"] != "read_in_full") & ~turns["eval_exclude"]].copy()
    pct_cols = [f"pct_{th}" for th in themes]
    pool["max_pct"] = pool[pct_cols].max(axis=1)

    n_seeded = int(n * SEEDED_SHARE)
    # Turns from interviews the old classifier never scored have no rank: they can be
    # drawn at random, but not for the seeded stratum.
    seeded_pool = pool[pool["max_pct"] >= 0.9].copy()
    # spread the seeded draw evenly over themes, so one big theme doesn't take it all
    seeded_pool["top_theme"] = seeded_pool[pct_cols].idxmax(axis=1).str.removeprefix("pct_")
    per_theme = n_seeded // len(themes)
    seeded = pd.concat([
        g.sample(min(per_theme, len(g)), random_state=seed)
        for _, g in seeded_pool.groupby("top_theme")
    ])
    seeded = seeded.assign(stratum="seeded")

    rest = pool.drop(seeded.index)
    n_random = n - len(seeded)
    share = rest["company"].value_counts(normalize=True)
    alloc = (share * n_random).round().astype(int).clip(lower=COMPANY_FLOOR)
    alloc = (alloc * n_random / alloc.sum()).round().astype(int)  # rescale back to n_random
    random_part = pd.concat([
        rest[rest["company"] == co].sample(min(k, (rest["company"] == co).sum()), random_state=seed)
        for co, k in alloc.items()
    ]).assign(stratum="random")

    sample = pd.concat([random_part, seeded]).sample(frac=1, random_state=seed)  # shuffle across batches
    sample["batch"] = [i // BATCH_SIZE for i in range(len(sample))]
    return sample


def clip_words(text, limit):
    words = str(text).split()
    return " ".join(words) if len(words) <= limit else " ".join(words[:limit]) + " [...]"


def render_turns(sample, all_turns):
    """The batch's turns as tagged text, each with the question it answers."""
    q_text = all_turns.set_index(["interview_id", "turn_id"])["body"]
    out = []
    for r in sample.itertuples():
        q = ""
        if pd.notna(r.question_turn_id) and str(r.question_turn_id).strip():
            q = q_text.get((r.interview_id, int(float(r.question_turn_id))), "")
        out.append(
            f'<turn id="{r.comment_id}" company="{r.company}" speaker="{r.speaker_name}" '
            f'date="{str(r.publish_date)[:10]}">\n'
            + (f"<question>{clip_words(q, MAX_QUESTION_WORDS)}</question>\n" if q else "")
            + f"<answer>{clip_words(r.body, MAX_TURN_WORDS)}</answer>\n</turn>"
        )
    return "\n\n".join(out)


# ---------------------------------------------------------------- prompts

def render_locked_themes(topics):
    blocks = []
    for t in topics:
        ex = [e for e in t["exemplars"] if e["usable"]]
        nm = [n for n in t["near_misses"] if n["usable"]]
        parts = [
            f'<theme id="{t["topic_id"]}" name="{t["name"]}">',
            f"<definition>{t['description']}</definition>",
            f"<include>{t['include']}</include>",
            f"<exclude>{t['exclude']}</exclude>",
        ]
        if t.get("boundary_notes"):
            parts.append(f"<boundary_notes>{t['boundary_notes']}</boundary_notes>")
        parts += [f"<example>{clip_words(e['text'], 150)}</example>" for e in ex]
        parts += [f"<not_this_theme>{clip_words(n['text'], 150)}</not_this_theme>" for n in nm]
        parts.append("</theme>")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def map_system(topics):
    return f"""You are helping a qualitative researcher build a codebook for a large interview corpus.

{common.STUDY_CONTEXT}

The researcher has already defined five themes. They are LOCKED: you must not redefine, rename, \
split or duplicate them. Each comes with examples, and with near misses -- passages that look \
like the theme but are not it.

<locked_themes>
{render_locked_themes(topics)}
</locked_themes>

Your task is discovery. You will receive a batch of executive answer turns. Propose candidate \
topics for what the executives talk about that the locked themes do NOT capture, so that \
eventually every substantive turn can be given a meaningful topic.

What a good candidate topic looks like:
- An analytic category of what the executive is talking about or doing in the turn, at roughly \
the same breadth as the locked themes. Not a named product, company or person ("GPT-4", \
"Microsoft"), and not a single anecdote.
- Recurring: it should plausibly apply to many turns across the corpus, not one.
- Distinct from every locked theme. If a candidate sits next to a locked theme, say which in \
borders_locked_themes and make the exclude criteria state the line between them.
- Defined so that two coders would apply it the same way: a definition (what it is), include \
(concrete signs a turn belongs), exclude (what looks similar but doesn't belong).

For each candidate give 2-5 example_comment_ids of turns from this batch that show it clearly \
(copy the id attribute exactly), and roughly how many turns in the batch it fits. Also report \
roughly how many turns fit each locked theme, and how many are non-substantive (greetings, \
thanks, logistics, fragments). Turns are cut at {MAX_TURN_WORDS} words, marked [...].

In notes, record anything a researcher should know: recurring material that resists \
categorization, or locked-theme boundaries that seem unclear in practice."""


def reduce_system(topics):
    return f"""You are helping a qualitative researcher build a codebook for a large interview corpus.

{common.STUDY_CONTEXT}

The researcher has five LOCKED themes, which you must not redefine, rename, split or duplicate:

<locked_themes>
{render_locked_themes(topics)}
</locked_themes>

A discovery pass read a sample of executive answer turns in batches, and proposed candidate \
topics for what the locked themes don't capture. Candidates from different batches overlap \
heavily and use different names for the same thing.

Your task: consolidate the candidates into {TARGET_TOPICS} emergent topics which, together with \
the five locked themes, give a meaningful topic to nearly every substantive executive turn.

- Merge candidates that describe the same phenomenon; record their names in merged_from.
- Keep topics at roughly the breadth of the locked themes. Prefer fewer, well-bounded topics \
over many thin ones, but don't force unrelated material together.
- Drop candidates that duplicate a locked theme, are too narrow to recur, or are not topics \
(e.g. greetings) -- list them in dropped_candidates with the reason.
- topic_id: lowercase snake_case, short, and different from every locked theme id.
- definition, include, exclude: written so two coders would apply the topic the same way. \
exclude must name the nearest other topics (locked or emergent) and state the line between them.
- boundary_notes: the hardest distinctions, especially against locked themes.
- example_comment_ids: 3-5 ids chosen from the merged candidates' examples (copy them exactly).
- approx_sample_turns: your estimate of how many sampled turns the topic fits, from the \
candidates' counts.

In notes, give an overall view: how much of the sample the final set covers, where coverage \
is weakest, and any locked-theme boundary that looked problematic."""


def reduce_user(candidates, locked_counts, sample_size, n_batches):
    lines = [f"The sample had {sample_size} turns in {n_batches} batches.",
             "Approximate turns per locked theme, summed over batches: "
             + ", ".join(f"{k}: {v}" for k, v in locked_counts.items()), "", "<candidates>"]
    for c in candidates:
        lines.append(json.dumps({k: c[k] for k in ("batch", "name", "definition", "include", "exclude",
                                                 "borders_locked_themes", "example_comment_ids",
                                                 "approx_turns_in_batch")}, ensure_ascii=False))
    lines.append("</candidates>")
    return "\n".join(lines)


# ---------------------------------------------------------------- main

def estimate_tokens(text):
    return len(text) / 4  # rough: ~4 characters per token for English


def main(run=False):
    started = datetime.now(timezone.utc)
    turns = pd.read_parquet(common.STAGE0_DIR / "turns_prepared.parquet")
    resolved = json.loads((common.STAGE0_DIR / "codebook_resolved.json").read_text(encoding="utf-8"))
    topics = resolved["topics"]
    themes = [t["topic_id"] for t in topics]
    if resolved["issues"]:
        print(f"note: stage 0 reported {len(resolved['issues'])} codebook issue(s) -- see stage0_report.md")

    sample = draw_sample(turns, themes)
    batches = [g for _, g in sample.groupby("batch")]
    print(f"sample: {len(sample)} turns ({sample['stratum'].value_counts().to_dict()}), "
          f"{sample['company'].value_counts().to_dict()}, {len(batches)} batches")

    out_dir = common.STAGE1_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    sample[["comment_id", "company", "stratum", "batch", "word_count"]].to_parquet(out_dir / "sample.parquet", index=False)

    system = map_system(topics)
    users = [render_turns(b, turns) for b in batches]
    sys_tok = estimate_tokens(system)
    user_tok = sum(estimate_tokens(u) for u in users)
    est = (sys_tok * PRICE_IN + sys_tok * (len(users) - 1) * PRICE_CACHE_READ + user_tok * PRICE_IN
           + len(users) * 6000 * PRICE_OUT + (30000 * PRICE_IN + 12000 * PRICE_OUT)) / 1e6
    print(f"map: system ~{sys_tok:,.0f} tokens (cached), turns ~{user_tok:,.0f} tokens; "
          f"estimated total cost incl. reduce and thinking: ~${est:.2f}")
    (out_dir / "map_system_prompt.txt").write_text(system, encoding="utf-8")
    (out_dir / "map_user_example.txt").write_text(users[0], encoding="utf-8")
    if not run:
        print(f"dry run: prompts written to {out_dir} (map_system_prompt.txt, map_user_example.txt). "
              f"Re-run with --run to call {common.DISCOVERY_MODEL}.")
        return

    import llm
    client = llm.client()

    def do_map(i):
        return llm.parse_call(out_dir / "raw" / f"map_{i:02d}.json", model=common.DISCOVERY_MODEL,
                              system=system, user=users[i], output_model=MapResult, client_=client)

    # First call alone writes the system-prompt cache; the rest can then read it.
    results = [do_map(0)]
    with ThreadPoolExecutor(WORKERS) as pool:
        results += list(pool.map(do_map, range(1, len(users))))
    print(f"map: {len(results)} calls done")

    candidates, locked_counts = [], {}
    for i, (res, _) in enumerate(results):
        for c in res.candidate_topics:
            candidates.append({"batch": i, **c.model_dump()})
        for lc in res.locked_theme_counts:
            locked_counts[lc.topic_id] = locked_counts.get(lc.topic_id, 0) + lc.approx_turns_in_batch
    common.write_json(out_dir / "candidates.json", {
        "candidates": candidates, "locked_theme_counts": locked_counts,
        "batch_notes": [r.notes for r, _ in results],
        "non_substantive": sum(r.approx_non_substantive_turns for r, _ in results),
    })
    print(f"map: {len(candidates)} candidate topics")

    reduced, reduce_record = llm.parse_call(
        out_dir / "raw" / "reduce.json", model=common.DISCOVERY_MODEL, system=reduce_system(topics),
        user=reduce_user(candidates, locked_counts, len(sample), len(batches)),
        output_model=ReduceResult, max_tokens=32000, cache_system=False, client_=client)

    write_draft(reduced, sample, turns, themes, out_dir, started)
    totals = llm.usage_totals([r for _, r in results] + [reduce_record])
    cost = (totals["input_tokens"] * PRICE_IN + totals["cache_read_input_tokens"] * PRICE_CACHE_READ
            + totals["cache_creation_input_tokens"] * PRICE_IN * 1.25 + totals["output_tokens"] * PRICE_OUT) / 1e6
    print(f"usage: {totals}; approx cost ${cost:.2f}")
    print(f"wrote {out_dir / 'emergent_topics_draft.json'} and discovery_review.md")


def write_draft(reduced, sample, turns, themes, out_dir, started):
    by_id = turns.set_index("comment_id")
    sampled = set(sample["comment_id"])
    used_ids = set(themes)
    draft, review, problems = [], [], []
    for t in reduced.emergent_topics:
        tid = re.sub(r"[^a-z0-9_]+", "_", t.topic_id.lower()).strip("_")
        if tid in used_ids:
            problems.append(f"topic_id `{tid}` collides with another topic; renamed `{tid}_2`")
            tid = f"{tid}_2"
        used_ids.add(tid)
        ex_ids = [c for c in t.example_comment_ids if c in sampled]
        for bad in set(t.example_comment_ids) - set(ex_ids):
            problems.append(f"{tid}: example id `{bad}` is not a sampled turn; dropped")
        exemplars = [{"comment_ids": [c], "company": by_id.at[c, "company"],
                      "person": by_id.at[c, "speaker_name"], "text": by_id.at[c, "body"]} for c in ex_ids]
        draft.append({
            "topic_id": tid, "name": t.name, "origin": "emergent", "locked": False,
            "description": t.definition, "include": t.include, "exclude": t.exclude,
            "boundary_notes": t.boundary_notes, "near_miss_comment_ids": [], "exemplars": exemplars,
            "merged_from": t.merged_from, "approx_sample_turns": t.approx_sample_turns,
        })
        review += [f"## {t.name} (`{tid}`)", "", f"~{t.approx_sample_turns} of {len(sample)} sampled turns. "
                   f"Merged from: {', '.join(t.merged_from)}", "",
                   f"**Definition.** {t.definition}", "", f"**Include.** {t.include}", "",
                   f"**Exclude.** {t.exclude}", "", f"**Boundary notes.** {t.boundary_notes}", "", "Examples:", ""]
        review += [f"- `{e['comment_ids'][0]}` ({e['company']}, {e['person']}): "
                   f"{clip_words(e['text'], 120)}" for e in exemplars]
        review.append("")

    proposal = {
        "codebook_version": "emergent-draft-1",
        "created_at": started.isoformat(timespec="seconds"),
        "model": common.DISCOVERY_MODEL,
        "sample_size": len(sample),
        "notes": reduced.notes,
        "dropped_candidates": [d.model_dump() for d in reduced.dropped_candidates],
        "problems": problems,
        "topics": draft,
    }
    # The model's proposal is always rewritten; the researcher's working copy only once.
    # Re-running this stage replays the recorded calls for free, and must never replace
    # the researcher's edits with the original proposal.
    common.write_json(out_dir / "emergent_topics_proposed.json", proposal)
    draft_path = out_dir / "emergent_topics_draft.json"
    if draft_path.exists():
        print(f"  kept your edited {draft_path.name}; the model's proposal is in emergent_topics_proposed.json")
    else:
        common.write_json(draft_path, proposal)
    header = [
        "# Discovery review: emergent topic draft", "",
        f"{len(draft)} emergent topics proposed by {common.DISCOVERY_MODEL} from {len(sample)} sampled "
        f"executive answer turns ({started:%Y-%m-%d}). Edit `emergent_topics_draft.json`, not this file.", "",
        "## For the researcher", "",
        "- Rename, merge, split or delete topics; tighten definitions and include/exclude.",
        "- Swap weak examples; add near misses (`near_miss_comment_ids`) where a boundary is hard.",
        "- Locked seed themes are not in this file; they stay in `stage0/codebook_seed_themes.json`.", "",
        "## Model's overall notes", "", reduced.notes, "",
    ]
    if problems:
        header += ["## Problems fixed automatically", ""] + [f"- {p}" for p in problems] + [""]
    dropped = ["## Dropped candidates", ""] + [f"- **{d.name}**: {d.reason}" for d in reduced.dropped_candidates]
    (out_dir / "discovery_review.md").write_text("\n".join(header + review + dropped) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run", action="store_true", help="call the API (default is a dry run)")
    main(run=parser.parse_args().run)
