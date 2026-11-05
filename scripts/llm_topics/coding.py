"""The coding step shared by the pilot (stage 2) and the full run (stage 3): the
combined codebook, the system prompt, how interviews are cut into requests, the
output schema, and the checks on what comes back. Keeping one copy is the point:
the pilot validates exactly the prompt the full run will use.

Codebook = the researcher's locked seed themes (stage0/codebook_resolved.json, built
from codebook_seed_themes.json) + the researcher-edited emergent topics
(stage1/emergent_topics_draft.json) + two fixed topics every turn can fall back to:
`other` (substantive, fits nothing) and `non_substantive`. codebook_version is a hash
of the rendered codebook, so every coded row can be traced to the exact definitions
it was coded against.

Requests are whole stretches of one interview, in spoken order: the executive answer
turns to code (code="yes") plus the questions and other turns around them as
context. A turn is never coded without the question it answers.
"""
import hashlib
import json
import re
from typing import Literal

import pandas as pd
from pydantic import create_model

import common

SEED_CODEBOOK = common.STAGE0_DIR / "codebook_resolved.json"
EMERGENT_CODEBOOK = common.STAGE1_DIR / "emergent_topics_draft.json"

MAX_TARGETS_PER_CHUNK = 25
MAX_TARGET_WORDS_PER_CHUNK = 5000
CONTEXT_TURNS_BEFORE = 4
CONTEXT_TURN_MAX_WORDS = 150     # context turns are cut; turns being coded never are
EXAMPLE_MAX_WORDS = 120
# Emergent topics show every example the researcher kept, up to this many. They are
# chosen deliberately -- a merged topic's examples cover each of its parts -- so a
# smaller cut would drop part of the definition. Seed themes show all of theirs.
EMERGENT_EXAMPLES = 10

# When a turn touches more than two topics, which two get the slots. The study's
# deductive (seed) themes are its analytic focus, so a clearly present seed theme
# takes a slot ahead of an emergent topic. Set False to rank purely by centrality.
PRIORITIZE_SEED_THEMES = True

SPECIAL_TOPICS = [
    {"topic_id": "other", "name": "Other (substantive)", "origin": "fixed",
     "description": "A substantive turn whose main point fits none of the topics above.",
     "include": "Real content -- an argument, explanation or story -- that no topic captures.",
     "exclude": "Anything a topic above fits, even loosely (use it, with lower confidence). "
                "Pleasantries and logistics (non_substantive).",
     "boundary_notes": "", "examples": [], "not_examples": []},
    {"topic_id": "non_substantive", "name": "Non-substantive", "origin": "fixed",
     "description": "The turn carries no topical content of its own.",
     "include": "Greetings, thanks, jokes, logistics, agreement or filler with nothing added, "
                "or a fragment cut off by transcription.",
     "exclude": "A short answer that does take a position on the question (code the topic, "
                "reading the question for context).",
     "boundary_notes": "", "examples": [], "not_examples": []},
]


def clip_words(text, limit):
    words = str(text).split()
    return " ".join(words) if len(words) <= limit else " ".join(words[:limit]) + " [...]"


def load_codebook(turns, seed_path=SEED_CODEBOOK, emergent_path=EMERGENT_CODEBOOK):
    """Returns (topics, version). topics: uniform dicts in prompt order -- seed themes,
    emergent topics, then the fixed ones."""
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    body = turns.set_index("comment_id")["body"]
    topics = []
    for t in seed["topics"]:
        topics.append({
            "topic_id": t["topic_id"], "name": t["name"], "origin": "seed",
            "description": t["description"], "include": t["include"], "exclude": t["exclude"],
            "boundary_notes": t.get("boundary_notes", ""),
            "examples": [e["text"] for e in t["exemplars"] if e["usable"]],
            "not_examples": [n["text"] for n in t["near_misses"] if n["usable"]],
            "example_comment_ids": [c for e in t["exemplars"] if e["usable"] for c in e["comment_ids"]],
            "near_miss_comment_ids": [n["comment_id"] for n in t["near_misses"] if n["usable"]],
        })
    if emergent_path is not None and emergent_path.exists():
        emergent = json.loads(emergent_path.read_text(encoding="utf-8"))
        for t in emergent["topics"]:
            ex = t.get("exemplars", [])[:EMERGENT_EXAMPLES]
            nm = [c for c in t.get("near_miss_comment_ids", []) if c in body.index]
            topics.append({
                "topic_id": t["topic_id"], "name": t["name"], "origin": "emergent",
                "description": t["description"], "include": t.get("include", ""),
                "exclude": t.get("exclude", ""), "boundary_notes": t.get("boundary_notes", ""),
                "examples": [e["text"] for e in ex],
                "not_examples": [body[c] for c in nm],
                "example_comment_ids": [c for e in ex for c in e["comment_ids"]],
                "near_miss_comment_ids": nm,
            })
    topics += [{**s, "example_comment_ids": [], "near_miss_comment_ids": []} for s in SPECIAL_TOPICS]
    ids = [t["topic_id"] for t in topics]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate topic_id(s) across seed and emergent codebooks: {sorted(dupes)}")
    rendered = render_codebook(topics)
    version = "cb_" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:12]
    return topics, version


def stale_references(topics):
    """snake_case words in definitions/criteria that look like topic ids but aren't one --
    usually a topic the researcher renamed, merged or deleted while another topic's
    exclude criteria still name it. The model would be pointed at a topic that doesn't
    exist."""
    ids = {t["topic_id"] for t in topics}
    found = []
    for t in topics:
        for field in ("description", "include", "exclude", "boundary_notes"):
            for word in set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", t.get(field) or "")):
                if word not in ids:
                    found.append(f"{t['topic_id']}.{field} mentions `{word}`, which is not a topic id")
    return sorted(found)


def shared_examples(topics):
    """Turns shown as an example of more than one topic. Fine when the turn genuinely
    carries both (it can be primary + secondary); a mixed signal when the topics are
    alternatives. A near miss for one topic that is an example of another is the
    intended contrast and isn't reported."""
    uses = {}
    for t in topics:
        for c in t["example_comment_ids"]:
            uses.setdefault(c, []).append(t["topic_id"])
    return sorted(f"turn {c} is an example for {' and '.join(ts)}" for c, ts in uses.items() if len(ts) > 1)


def render_codebook(topics):
    blocks = []
    for t in topics:
        kind = {"seed": "researcher theme", "emergent": "emergent topic", "fixed": "fallback"}[t["origin"]]
        parts = [f'<topic id="{t["topic_id"]}" name="{t["name"]}" kind="{kind}">',
                 f"<definition>{t['description']}</definition>"]
        if t["include"]:
            parts.append(f"<include>{t['include']}</include>")
        if t["exclude"]:
            parts.append(f"<exclude>{t['exclude']}</exclude>")
        if t["boundary_notes"]:
            parts.append(f"<boundary_notes>{t['boundary_notes']}</boundary_notes>")
        parts += [f"<example>{clip_words(x, EXAMPLE_MAX_WORDS)}</example>" for x in t["examples"]]
        parts += [f"<not_this_topic>{clip_words(x, EXAMPLE_MAX_WORDS)}</not_this_topic>" for x in t["not_examples"]]
        parts.append("</topic>")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def system_prompt(topics):
    seed_rule = (
        "\n- If a researcher theme is clearly present (a real part of the turn, not a passing "
        "word), it must take the primary or secondary slot, ahead of an emergent topic. The "
        "researcher themes are the study's focus."
        if PRIORITIZE_SEED_THEMES else "")
    return f"""You are a careful qualitative coder applying a fixed codebook to interview transcripts.

{common.STUDY_CONTEXT}

<codebook>
{render_codebook(topics)}
</codebook>

Each topic has a definition, include and exclude criteria, and sometimes boundary notes. \
<example> passages are clear cases of the topic; <not_this_topic> passages look similar but \
are not it -- they mark the boundary.

You will receive part of one interview transcript. Code every turn marked code="yes" -- an \
executive's answer -- exactly once. Other turns (questions, earlier turns, other speakers) are \
context only: never code them, but use them to understand what the executive is responding to.

For each coded turn:
- topic_summary: what the executive says, in at most 25 words. Describe the content, not \
the topic label.
- justification: at most 30 words on why the turn fits its topic(s). Point to what the \
executive actually says, and for a close call name the topic you ruled out.
- primary_topic_id: the topic that best captures the main point of the turn -- what most of \
it is doing.
- secondary_topic_id: a second topic that is also clearly present (a real part of the turn, \
not a passing mention), or "none".
- confidence: how sure you are of the primary topic, from 0 to 1. 0.9 or above: a clear, \
typical case. About 0.7: fits well, with some ambiguity. About 0.5: borderline between two \
topics. Below 0.4: a guess.

Rules:
- Apply the definitions and include/exclude criteria as written; when they conflict with your \
own sense of a word, the codebook wins.
- Code what the executive says, not the interviewer's framing. A short answer that only \
agrees or disagrees takes its topic from the question it answers; one that adds nothing is \
non_substantive.
- Prefer a topic that fits, even loosely, over `other`; then lower the confidence.
- If more than two topics apply, keep the two most central.{seed_rule}
- secondary_topic_id must differ from primary_topic_id.
- Return one entry per coded turn, with comment_id copied exactly from the turn's id."""


def output_model(topic_ids):
    """Pydantic schema for one request's answer. topic ids are an enum, so the model
    can't invent one."""
    primary = Literal[tuple(topic_ids)]
    secondary = Literal[tuple(topic_ids) + ("none",)]
    turn_code = create_model(
        "TurnCode",
        comment_id=(str, ...), topic_summary=(str, ...), justification=(str, ...),
        primary_topic_id=(primary, ...), secondary_topic_id=(secondary, ...), confidence=(float, ...),
    )
    return create_model("ChunkCodes", codes=(list[turn_code], ...))


def chunk_interview(g):
    """Split one interview (rows in spoken order) into requests. Returns a list of
    (start_pos, end_pos, target_comment_ids) over g's positions."""
    targets = [i for i, s in enumerate(g["coded_scope"]) if s == "llm"]
    words = g["word_count"].tolist()
    chunks, cur, cur_words = [], [], 0
    for i in targets:
        if cur and (len(cur) >= MAX_TARGETS_PER_CHUNK or cur_words + words[i] > MAX_TARGET_WORDS_PER_CHUNK):
            chunks.append(cur)
            cur, cur_words = [], 0
        cur.append(i)
        cur_words += words[i]
    if cur:
        chunks.append(cur)
    ids = g["comment_id"].tolist()
    out = []
    for n, c in enumerate(chunks):
        start = 0 if n == 0 else max(0, c[0] - CONTEXT_TURNS_BEFORE)
        out.append((start, c[-1], [ids[i] for i in c]))
    return out


def render_chunk(g, start, end, target_ids):
    first = g.iloc[0]
    targets = set(target_ids)
    lines = [f'<interview title="{first["interview_title"]}" executive="{first["featured_executive"]}" '
             f'company="{first["company"]}" date="{str(first["publish_date"])[:10]}" '
             f'channel="{first["channel_name"]}">', "<transcript>"]
    if start > 0:
        lines.append("[... earlier turns omitted ...]")
    for r in g.iloc[start:end + 1].itertuples():
        if r.comment_id in targets:
            lines.append(f'<turn id="{r.comment_id}" speaker="{r.speaker_name}" role="answer" code="yes">'
                         f"{r.body}</turn>")
        else:
            lines.append(f'<turn speaker="{r.speaker_name}" role="{r.speaker_role}">'
                         f"{clip_words(r.body, CONTEXT_TURN_MAX_WORDS)}</turn>")
    lines += ["</transcript>", "</interview>", "",
              f"Code these {len(target_ids)} turns, each exactly once: {', '.join(target_ids)}"]
    return "\n".join(lines)


def build_requests(turns, interview_ids):
    """[(request_key, interview_id, target_ids, user_text)] for the given interviews."""
    reqs = []
    sub = turns[turns["interview_id"].isin(interview_ids)].sort_values(["interview_id", "turn_id"])
    for iid, g in sub.groupby("interview_id", sort=True):
        g = g.reset_index(drop=True)
        for n, (start, end, ids) in enumerate(chunk_interview(g)):
            reqs.append((f"{iid}_{n:02d}", iid, ids, render_chunk(g, start, end, ids)))
    return reqs


def check_codes(parsed, target_ids):
    """Split a response into (rows for the requested turns, missing ids, notes).
    Extra or repeated ids are dropped; secondary == primary becomes "none"."""
    wanted, rows, notes = set(target_ids), {}, []
    for c in parsed.codes:
        d = c.model_dump()
        if d["comment_id"] not in wanted:
            notes.append(f"unrequested id {d['comment_id']} dropped")
            continue
        if d["comment_id"] in rows:
            notes.append(f"duplicate id {d['comment_id']}: kept the first")
            continue
        if d["secondary_topic_id"] == d["primary_topic_id"]:
            d["secondary_topic_id"] = "none"
        d["confidence"] = min(1.0, max(0.0, float(d["confidence"])))
        rows[d["comment_id"]] = d
    missing = [i for i in target_ids if i not in rows]
    return rows, missing, notes


def rule_rows(turns, interview_ids, codebook_version):
    """Executive turns under MIN_CODED_WORDS words: non_substantive by rule, no API call."""
    r = turns[turns["interview_id"].isin(interview_ids) & (turns["coded_scope"] == "rule_non_substantive")]
    return pd.DataFrame({
        "comment_id": r["comment_id"], "topic_summary": "", "justification":
        f"Executive turn under {common.MIN_CODED_WORDS} words; coded by rule, not by the model.",
        "primary_topic_id": "non_substantive", "secondary_topic_id": "none", "confidence": 1.0,
        "coded_by": "rule", "codebook_version": codebook_version,
    })
