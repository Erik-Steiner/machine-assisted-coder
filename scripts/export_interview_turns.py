"""Exports company interview datasets (queries/company_*.json) as one-row-per-turn
CSVs in the same column layout as the reddit comment export another project's
pipeline already reads (data examples/reddit_example.xlsx): the interview plays
the role of the reddit post, each speaker turn the role of a comment. The 27
reddit columns come first, in their original order, so that pipeline can load
these files unchanged; interview-specific columns are appended after them.

Question/answer linking is by speaker role, not by reading the text: a turn by
any featured executive in these datasets is an "answer", a narrator or
unidentifiable speaker is "other", everyone else (interviewer, host, audience)
is a "question". Each answer points at the most recent question before it, so
an executive's answer split across several consecutive turns all link to the
same question. turn_id preserves the original order regardless, for anyone who
wants to re-pair turns differently.

The same video is sometimes held twice, once per featured executive (e.g. Dario
and Daniela Amodei at the same event), each with its own item_id and its own
independently transcribed turns. Both are kept; shared_source_url_with names
the other interview_id(s) so the downstream researcher can decide.

Read-only against queries/ -- never touches coding.db.

Usage:
    python scripts/export_interview_turns.py
    python scripts/export_interview_turns.py --out-dir some/other/folder
"""
import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths  # noqa: E402

# company label -> dataset filename stem in queries/
DATASETS = {
    "OpenAI": "company_OpenAI_20260731_171118",
    "Anthropic": "company_Anthropic_20260731_171118",
    "Google": "company_Google_LLC_20260731_171118",
    "DeepMind": "company_DeepMind_20260731_171118",
}

REDDIT_COLUMNS = [
    "source_platform", "source_file", "source_file_path", "global_post_key",
    "comment_id", "post_id", "parent_id", "reply_type", "parent_comment_id",
    "parent_post_id", "body", "created_utc_comment", "created_date", "year",
    "author_comment", "score_comment", "post_title", "selftext",
    "created_utc_post", "author_post", "score_post", "num_comments",
    "permalink_comment", "permalink_post", "domain", "ups_comment", "ups_post",
]
EXTRA_COLUMNS = [
    "interview_id", "turn_id", "speaker_name", "speaker_id", "speaker_role",
    "question_turn_id", "turn_timestamp", "turn_offset_secs", "word_count",
    "company", "featured_executive", "executive_title", "executive_id",
    "interview_title", "source_url", "channel_name", "publish_date",
    "duration_secs", "view_count", "like_count", "combined_classifier_score",
    "n_turns", "shared_source_url_with",
]

# Speakers we can't attribute either way -- not a question, not an executive answer.
OTHER_SPEAKER = re.compile(r"narrator|^unknown|^speaker\s*\d+$", re.IGNORECASE)
TIMESTAMP = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")


def parse_timestamp(raw):
    """Normalize to HH:MM:SS. 137 of ~64k source timestamps carry SRT-style
    milliseconds or trailing text ("00:04:15,", "00:01:02,345 --> ..."); the
    first H:MM:SS in the string is the turn's start. A bare MM:SS is minutes."""
    m = TIMESTAMP.search(raw or "")
    if not m:
        return "", ""
    h, mi, s = (m.group(1), m.group(2), m.group(3)) if m.group(3) else ("0", m.group(1), m.group(2))
    secs = int(h) * 3600 + int(mi) * 60 + int(s)
    return f"{int(h):02d}:{int(mi):02d}:{int(s):02d}", secs


def speaker_role(name, executives):
    if name in executives:
        return "answer"
    if OTHER_SPEAKER.search(name.strip()):
        return "other"
    return "question"


def interview_rows(rec, company, source_file, source_path, executives, shared_with):
    iid = str(rec["item_id"])
    post_key = f"{company}__{iid}"
    pub = rec.get("publish_date") or ""
    pub_dt = pub[:19].replace("T", " ")
    url = rec.get("source_url") or ""
    turns = rec.get("turns") or []

    rows = []
    last_question = None  # comment_id/turn_id of the most recent question turn
    prev_comment = None
    for i, t in enumerate(turns, start=1):
        cid = f"{iid}_{i:04d}"
        name = (t.get("speaker_name") or "").strip()
        role = speaker_role(name, executives)
        ts, secs = parse_timestamp(t.get("timestamp"))
        body = t.get("content") or ""

        if role == "answer" and last_question:
            parent = last_question[0]
        else:
            parent = prev_comment
        question_turn_id = last_question[1] if role == "answer" and last_question else ""

        row = {
            "source_platform": company,
            "source_file": source_file,
            "source_file_path": source_path,
            "global_post_key": post_key,
            "comment_id": cid,
            "post_id": iid,
            "parent_id": parent or iid,
            "reply_type": role,
            "parent_comment_id": parent or "",
            "parent_post_id": "" if parent else iid,
            "body": body,
            "created_utc_comment": ts,
            "created_date": pub[:10],
            "year": pub[:4],
            "author_comment": name,
            "score_comment": "",
            "post_title": rec.get("item_title") or "",
            "selftext": "",
            "created_utc_post": pub_dt,
            "author_post": rec.get("person_name") or "",
            "score_post": rec.get("view_count") or "",
            "num_comments": len(turns),
            "permalink_comment": f"{url}&t={secs}s" if "youtube.com/watch" in url and secs != "" else url,
            "permalink_post": url,
            "domain": rec.get("source_name") or "",
            "ups_comment": "",
            "ups_post": rec.get("like_count") if rec.get("like_count") is not None else "",
            # interview-specific
            "interview_id": iid,
            "turn_id": i,
            "speaker_name": name,
            "speaker_id": t.get("speaker_id") if t.get("speaker_id") is not None else "",
            "speaker_role": role,
            "question_turn_id": question_turn_id,
            "turn_timestamp": ts,
            "turn_offset_secs": secs,
            "word_count": len(body.split()),
            "company": company,
            "featured_executive": rec.get("person_name") or "",
            "executive_title": rec.get("person_title") or "",
            "executive_id": rec.get("person_id") if rec.get("person_id") is not None else "",
            "interview_title": rec.get("item_title") or "",
            "source_url": url,
            "channel_name": rec.get("source_name") or "",
            "publish_date": pub[:10],
            "duration_secs": rec.get("duration_secs") if rec.get("duration_secs") is not None else "",
            "view_count": rec.get("view_count") or "",
            "like_count": rec.get("like_count") if rec.get("like_count") is not None else "",
            "combined_classifier_score": rec.get("combined_classifier_score") if rec.get("combined_classifier_score") is not None else "",
            "n_turns": len(turns),
            "shared_source_url_with": ";".join(x for x in shared_with if x != iid),
        }
        rows.append(row)
        if role == "question":
            last_question = (cid, i)
        prev_comment = cid
    return rows


def write_csv(path, rows):
    # utf-8-sig so Excel shows names/em-dashes correctly instead of mojibake
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=REDDIT_COLUMNS + EXTRA_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", type=Path, default=paths.EXPORTS_DIR / "interview_turns")
    args = ap.parse_args()

    data = {}
    for company, stem in DATASETS.items():
        path = paths.QUERIES_DIR / f"{stem}.json"
        with open(path, encoding="utf-8") as f:
            data[company] = (path, json.load(f))

    executives = {r["person_name"] for _, recs in data.values() for r in recs if r.get("person_name")}
    by_url = defaultdict(list)
    for _, recs in data.values():
        for r in recs:
            by_url[r.get("source_url") or ""].append(str(r["item_id"]))

    args.out_dir.mkdir(parents=True, exist_ok=True)
    all_rows = []
    for company, (path, recs) in data.items():
        rows = []
        for r in recs:
            shared = by_url[r.get("source_url") or ""] if r.get("source_url") else []
            rows.extend(interview_rows(r, company, path.stem, str(path), executives, shared))
        write_csv(args.out_dir / f"{company}_interview_turns.csv", rows)
        all_rows.extend(rows)
        print(f"{company}: {len(recs)} interviews, {len(rows)} turns")
    write_csv(args.out_dir / "all_companies_interview_turns.csv", all_rows)
    print(f"all: {len(all_rows)} turns -> {args.out_dir}")


if __name__ == "__main__":
    main()
