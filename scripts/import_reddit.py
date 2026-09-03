"""Imports Reddit data from Arctic Shift's export shape (JSONL: one JSON
object per line, submissions and comments as separate files) into queries/,
in the same JSON+CSV shape every other dataset uses.

One canonical "item" per submission, with the submission body as turn 0 and
its comments as subsequent turns, ordered by a depth-first walk of the real
reply tree (reconstructed from each comment's parent_id/link_id) -- not flat
chronological order. Most Reddit comments reply to another comment, not the
submission itself, so a flat ordering would misrepresent the conversation;
each turn's `depth` (0 = submission, 1 = top-level comment, 2+ = nested
reply) is what the Coding tab uses to render the thread indented. Siblings
(replies to the same parent) are ordered chronologically (oldest first).

speaker_role is left null for every Reddit turn: Reddit has no
interviewer/subject structure, so every turn -- submission and comments
alike -- is ordinary codable/trainable content by default (see
IMPORTING_DATA.md; the speaker_role exclusion is opt-in per source, not
something Reddit data needs).

Usage:
    python scripts/import_reddit.py --submissions posts.jsonl --comments comments.jsonl
    python scripts/import_reddit.py --submissions posts.jsonl   # submissions only
"""
import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import query_api
from paths import QUERIES_DIR

DELETED_BODIES = {"[deleted]", "[removed]"}


def parse_jsonl_text(text):
    """One JSON object per line -> list of dicts. Shared by read_jsonl() (CLI,
    reads from disk) and viewer_server.py's import endpoints (already has the
    decoded text from an uploaded file, no path on disk to read)."""
    records = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def read_jsonl(path):
    return parse_jsonl_text(path.read_text(encoding="utf-8"))


def to_iso(unix_secs):
    if unix_secs is None:
        return None
    return datetime.fromtimestamp(unix_secs, tz=timezone.utc).isoformat()


def build_comment_tree(comments, submission_id):
    """{parent_key: [comment, ...]} where parent_key is either the submission's
    own id (top-level comments) or another comment's id (nested replies),
    each bucket sorted chronologically. parent_id/link_id carry a "t1_"/"t3_"
    type prefix Reddit uses to distinguish comment vs. submission ids --
    stripped here since our own ids don't need it."""
    by_parent = {}
    for c in comments:
        parent_id = c.get("parent_id") or ""
        parent_key = parent_id.split("_", 1)[-1] if "_" in parent_id else parent_id
        by_parent.setdefault(parent_key, []).append(c)
    for bucket in by_parent.values():
        bucket.sort(key=lambda c: c.get("created_utc") or 0)
    return by_parent


def walk_comments(by_parent, parent_key, depth, out):
    for c in by_parent.get(parent_key, []):
        body = c.get("body")
        if body in DELETED_BODIES:
            # Kept as a placeholder turn (not skipped) so any real replies
            # underneath it keep correct depth/context -- the word-count
            # filter naturally deprioritizes these low-content turns later.
            body = body or "[deleted]"
        out.append({
            "speaker_name": c.get("author"),
            "speaker_id": c.get("author_fullname"),
            "speaker_role": None,
            "depth": depth,
            "timestamp": to_iso(c.get("created_utc")),
            "content": body or "",
        })
        walk_comments(by_parent, c.get("id"), depth + 1, out)


def build_record(submission, by_parent):
    item_id = str(submission["id"])
    turns = []
    selftext = submission.get("selftext") or ""
    if selftext.strip():
        turns.append({
            "speaker_name": submission.get("author"),
            "speaker_id": submission.get("author_fullname"),
            "speaker_role": None,
            "depth": 0,
            "timestamp": to_iso(submission.get("created_utc")),
            "content": selftext,
        })
    walk_comments(by_parent, item_id, 1, turns)

    permalink = submission.get("permalink") or ""
    return {
        "group_name": submission.get("subreddit"),
        "person_name": submission.get("author"),
        "person_title": None,
        "person_id": submission.get("author_fullname"),
        "item_id": item_id,
        "item_title": submission.get("title"),
        "source_url": f"https://reddit.com{permalink}" if permalink else None,
        "publish_date": to_iso(submission.get("created_utc")),
        "duration_secs": None,
        "view_count": None,
        "like_count": submission.get("score"),
        "combined_classifier_score": None,
        "source_name": "Reddit",
        "transcript_text": "\n".join(
            f"[{t.get('speaker_name') or 'unknown'}] {t['content'].strip()}" for t in turns if t.get("content")
        ),
        "turns": turns,
    }


def build_records(submissions, comments):
    """submissions/comments: already-parsed lists of dicts (see read_jsonl/
    parse_jsonl_text). Links each comment to its submission via link_id,
    walks each submission's reply tree, and builds one canonical record per
    submission. Returns (records, orphan_count) -- shared by the CLI's
    main() and viewer_server.py's /api/import/reddit/parse endpoint."""
    submission_ids = {str(s["id"]) for s in submissions}
    by_submission = {}
    orphans = 0
    for c in comments:
        link_id = c.get("link_id") or ""
        sid = link_id.split("_", 1)[-1] if "_" in link_id else link_id
        if sid not in submission_ids:
            orphans += 1
            continue
        by_submission.setdefault(sid, []).append(c)

    records = []
    for s in submissions:
        sid = str(s["id"])
        by_parent = build_comment_tree(by_submission.get(sid, []), sid)
        records.append(build_record(s, by_parent))
    return records, orphans


def summarize_records(records):
    """(n_turns, subreddits sorted, label) for a built records list -- shared
    by the CLI's summary print and viewer_server.py's parse-preview response."""
    n_turns = sum(len(r["turns"]) for r in records)
    subreddits = sorted({r["group_name"] for r in records if r["group_name"]})
    label = subreddits[0] if len(subreddits) == 1 else f"{len(subreddits)} subreddits"
    return n_turns, subreddits, label


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--submissions", required=True, help="Arctic Shift submissions JSONL export")
    parser.add_argument("--comments", help="Arctic Shift comments JSONL export (optional)")
    args = parser.parse_args()

    submissions_path = Path(args.submissions)
    if not submissions_path.exists():
        print(f"File not found: {submissions_path}")
        sys.exit(1)
    submissions = read_jsonl(submissions_path)
    if not submissions:
        print(f"No submissions found in {submissions_path}")
        sys.exit(1)

    comments = []
    if args.comments:
        comments_path = Path(args.comments)
        if not comments_path.exists():
            print(f"File not found: {comments_path}")
            sys.exit(1)
        comments = read_jsonl(comments_path)

    records, orphans = build_records(submissions, comments)
    if orphans:
        print(f"Skipped {orphans} comment(s) whose submission wasn't in {submissions_path.name}")

    query_api.validate_dataset_records(records, submissions_path.name, require_item_id=True)

    n_turns, subreddits, label = summarize_records(records)
    print(f"Built {len(records)} item(s), {n_turns} turn(s) total, from r/{label}"
          if len(subreddits) == 1 else f"Built {len(records)} item(s), {n_turns} turn(s) total, across {label}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base_name = f"reddit_{query_api.slugify(label)}_{stamp}"
    json_path, csv_path = query_api.write_export(QUERIES_DIR, base_name, records)
    query_api.register_export(
        QUERIES_DIR,
        dataset_id=f"q_{uuid.uuid4().hex[:10]}",
        label=f"r/{label} — {len(records)} submission(s)",
        kind="reddit_import",
        source_id=None,
        created_at=datetime.now(timezone.utc).isoformat(),
        count=len(records),
        json_path=json_path,
        csv_path=csv_path,
    )
    print(f"\nSaved {len(records)} record(s) to {json_path.name} / {csv_path.name}")
    print("Run scripts/build_segments.py next to make this dataset codeable.")


if __name__ == "__main__":
    main()
