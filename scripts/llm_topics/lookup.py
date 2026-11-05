"""Find the ids for a passage, to put it in a codebook file as an exemplar or near miss.

The app's Code tab shows an interview as `dataset_id:item_id` (e.g. q_61e9e49b4f:103808)
but no turn or segment ids, and discovery_review.md shows turn ids (comment_id, e.g.
447648_0002). This prints, for a passage, everything a codebook entry can use:
  comment_id   the turn -- what an exemplar ({"comment_id": ...}) or a near miss
               (near_miss_comment_ids) takes
  segment_ids  the app's segments in that turn (a long turn is split into several)
  gold labels  per seed theme, from stage0/turns_prepared.parquet -- run stage 0 first
plus the turn's speaker, role and opening words, so you can check it's the right one.

Usage:
    python scripts/llm_topics/lookup.py 447648_0002 1052688_0012            # turn ids
    python scripts/llm_topics/lookup.py q_61e9e49b4f:103808 --text "race to the top"
    python scripts/llm_topics/lookup.py 103808 --text "race to the top"     # item_id alone works too
    python scripts/llm_topics/lookup.py 103808                              # list the interview's answer turns
"""
import argparse
import re

import pandas as pd

import common

PREVIEW_WORDS = 30


def parse_interview(arg):
    """'q_61e9e49b4f:103808', '103808' -> 103808 (the turn exports' interview_id)."""
    m = re.fullmatch(r"(?:[A-Za-z0-9_]+:)?(\d+)", arg.strip())
    return int(m.group(1)) if m else None


def show(rows, themes, tmap):
    segs = tmap.groupby(["interview_id", "turn_id"])["segment_id"].apply(list)
    for r in rows.itertuples():
        labels = ", ".join(f"{th}={getattr(r, 'gold_' + th)}" for th in themes
                           if getattr(r, "gold_" + th) != "neutral") or "no seed-theme labels (neutral)"
        words = str(r.body).split()
        preview = " ".join(words[:PREVIEW_WORDS]) + (" ..." if len(words) > PREVIEW_WORDS else "")
        print(f"comment_id  {r.comment_id}")
        print(f"  interview {r.interview_id} ({r.company}, {str(r.publish_date)[:10]}) -- {r.interview_title}")
        print(f"  speaker   {r.speaker_name} [{r.speaker_role}], {r.word_count} words, scope {r.coded_scope}")
        print(f"  segments  {', '.join(segs.get((r.interview_id, r.turn_id), []))}")
        print(f"  labels    {labels}")
        if r.exemplar_for or r.near_miss_for:
            print(f"  already   exemplar for: {r.exemplar_for or '-'}; near miss for: {r.near_miss_for or '-'}")
        print(f"  text      {preview}\n")


def main(ids, text):
    turns = pd.read_parquet(common.STAGE0_DIR / "turns_prepared.parquet")
    themes = [c.removeprefix("gold_") for c in turns.columns if c.startswith("gold_") and c != "gold_set"]
    from stage0_prepare import build_turn_segment_map

    hits = []
    for arg in ids:
        if re.fullmatch(r"\d+_\d{4}", arg):
            sel = turns[turns["comment_id"] == arg]
            if sel.empty:
                print(f"{arg}: no such turn\n")
        else:
            iid = parse_interview(arg)
            sel = turns[turns["interview_id"] == iid] if iid is not None else turns.iloc[0:0]
            if sel.empty:
                print(f"{arg}: no such interview in the turn exports\n")
                continue
            if text:
                needle = re.sub(r"\s+", " ", text.lower()).strip()
                sel = sel[sel["body"].str.lower().str.replace(r"\s+", " ", regex=True).str.contains(needle, regex=False)]
                if sel.empty:
                    print(f"{arg}: no turn contains \"{text}\" (matching ignores case and spacing, not punctuation)\n")
            else:
                sel = sel[sel["speaker_role"] == "answer"]
        hits.append(sel)
    rows = pd.concat(hits) if hits else turns.iloc[0:0]
    if rows.empty:
        return
    tmap = build_turn_segment_map(turns[turns["interview_id"].isin(rows["interview_id"].unique())])
    show(rows, themes, tmap)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ids", nargs="+", help="turn ids (447648_0002) or interviews (q_...:103808 or 103808)")
    parser.add_argument("--text", help="words from the passage, to find the turn within an interview")
    args = parser.parse_args()
    main(args.ids, args.text)
