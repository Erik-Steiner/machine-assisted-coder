"""Turn an edited discovery_review.md back into emergent_topics_draft.json.

The review file is easier to edit than the JSON -- one section per topic, in prose --
so a researcher may well rename, merge, delete and rewrite topics there. Later stages
read only the JSON; this carries the edits across. What it reads, per `## ` section
between "Model's overall notes" and "Dropped candidates":

  ## <name> (`<topic_id>`)                 name and id (rename either here)
  ~<n> of ... Merged from: <text>           kept as approx_sample_turns / merged_from
  **Definition.** / **Include.** / **Exclude.** / **Boundary notes.**   one paragraph each
  **Near misses.** `id`, `id`               optional: turns that look like the topic but aren't
  - `<comment_id>` (...): <preview>         an example turn; the full turn text comes from
                                            stage0's turn table, not the clipped preview

A section deleted from the file is a topic deleted from the draft. The previous draft
is kept as emergent_topics_draft.before_<timestamp>.json, so nothing is lost.

Run the stage 2 dry run afterwards: it lists stale topic references (an exclude
criterion still naming a merged or deleted topic) and turns used as examples twice.

Usage:
    python scripts/llm_topics/review_to_draft.py                  # writes the draft
    python scripts/llm_topics/review_to_draft.py --check          # only report what it would write
"""
import argparse
import json
import re
from datetime import datetime, timezone

import pandas as pd

import common

REVIEW = common.STAGE1_DIR / "discovery_review.md"
DRAFT = common.STAGE1_DIR / "emergent_topics_draft.json"

HEADING = re.compile(r"^## (.+?) \(`([a-z0-9_]+)`\)\s*$")
FIELDS = {"Definition": "description", "Include": "include", "Exclude": "exclude",
          "Boundary notes": "boundary_notes", "Near misses": "near_misses"}
FIELD_LINE = re.compile(r"^\*\*(Definition|Include|Exclude|Boundary notes|Near misses)\.\*\*\s*(.*)$")
EXAMPLE_LINE = re.compile(r"^- `(\d+_\d{4})`")
COUNT_LINE = re.compile(r"^~(\d+) of \d+ sampled turns\.\s*(?:Merged from:\s*(.*))?$")


def parse_review(text):
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("## Model's overall notes"))
    except StopIteration:
        start = 0
    end = next((i for i, l in enumerate(lines) if l.startswith("## Dropped candidates")), len(lines))
    topics, cur, problems = [], None, []
    for line in lines[start:end]:
        h = HEADING.match(line)
        if h:
            cur = {"topic_id": h.group(2), "name": h.group(1).strip(), "description": "", "include": "",
                   "exclude": "", "boundary_notes": "", "near_misses": "", "examples": [],
                   "approx_sample_turns": None, "merged_from": ""}
            topics.append(cur)
            continue
        if cur is None:
            continue
        line = line.strip()
        if m := FIELD_LINE.match(line):
            cur[FIELDS[m.group(1)]] = m.group(2).strip()
        elif m := EXAMPLE_LINE.match(line):
            cur["examples"].append(m.group(1))
        elif m := COUNT_LINE.match(line):
            cur["approx_sample_turns"] = int(m.group(1))
            cur["merged_from"] = (m.group(2) or "").strip()
        elif line.startswith("## "):
            problems.append(f"heading not read as a topic (needs `## Name (\\`topic_id\\`)`): {line}")
    return topics, problems


def main(check=False):
    topics, problems = parse_review(REVIEW.read_text(encoding="utf-8"))
    turns = pd.read_parquet(common.STAGE0_DIR / "turns_prepared.parquet").set_index("comment_id")
    old = json.loads(DRAFT.read_text(encoding="utf-8")) if DRAFT.exists() else {"topics": []}
    old_ids = [t["topic_id"] for t in old["topics"]]

    ids = [t["topic_id"] for t in topics]
    for d in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"topic_id `{d}` appears more than once")
    draft = []
    for t in topics:
        exemplars = []
        for cid in t["examples"]:
            if cid not in turns.index:
                problems.append(f"{t['topic_id']}: example {cid} isn't a turn in the exports; dropped")
                continue
            r = turns.loc[cid]
            exemplars.append({"comment_ids": [cid], "company": r["company"], "person": r["speaker_name"],
                              "text": r["body"]})
        near = re.findall(r"\d+_\d{4}", t["near_misses"])
        for cid in near:
            if cid not in turns.index:
                problems.append(f"{t['topic_id']}: near miss {cid} isn't a turn in the exports")
        for f in ("description", "include", "exclude"):
            if not t[f]:
                problems.append(f"{t['topic_id']}: `{f}` is empty")
        if not exemplars:
            problems.append(f"{t['topic_id']}: no examples")
        draft.append({
            "topic_id": t["topic_id"], "name": t["name"], "origin": "emergent", "locked": False,
            "description": t["description"], "include": t["include"], "exclude": t["exclude"],
            "boundary_notes": t["boundary_notes"], "near_miss_comment_ids": [c for c in near if c in turns.index],
            "exemplars": exemplars, "merged_from": t["merged_from"],
            "approx_sample_turns": t["approx_sample_turns"],
        })

    removed = [i for i in old_ids if i not in ids]
    added = [i for i in ids if i not in old_ids]
    print(f"{REVIEW.name}: {len(draft)} topics ({sum(len(t['exemplars']) for t in draft)} examples). "
          f"Removed vs current draft: {removed or 'none'}. New ids: {added or 'none'}.")
    for t in draft:
        print(f"  {t['topic_id']:32s} {t['name'][:60]:60s} examples={len(t['exemplars'])} "
              f"near_misses={len(t['near_miss_comment_ids'])}")
    for p in problems:
        print(f"  problem: {p}")
    if check:
        print("check only: nothing written")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if DRAFT.exists():
        backup = DRAFT.with_name(f"emergent_topics_draft.before_{stamp}.json")
        backup.write_text(DRAFT.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"previous draft kept as {backup.name}")
    common.write_json(DRAFT, {
        **{k: v for k, v in old.items() if k != "topics"},
        "codebook_version": f"emergent-review-{stamp}",
        "edited_in": REVIEW.name,
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "topics": draft,
    })
    print(f"wrote {DRAFT}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="report only; don't write the draft")
    main(check=parser.parse_args().check)
