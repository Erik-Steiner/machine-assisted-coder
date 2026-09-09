"""Imports an interview transcript into queries/, in the same JSON+CSV shape
every other dataset uses (see query_api.EXPORT_FIELDS / IMPORTING_DATA.md).

Two input modes, chosen by file extension:

  .docx / .txt  -- auto-detected against two deterministic formats (no LLM
                   needed for either -- see parse_transcript_turns()):

                   1. Word "Transcribe"-style: repeated "HH:MM:SS[ Speaker
                      Label]" lines, each followed by that turn's text.
                   2. Speaker-labeled, no timestamp: repeated "Speaker: text"
                      (optionally "**Speaker:**" markdown-bold) lines/
                      paragraphs -- covers hand-typed transcripts,
                      diarization-tool exports, and typical ChatGPT/Claude
                      "format this as an interview" output alike (see
                      IMPORTING_DATA.md for the full writeup and examples).

                   Either way you'll be asked (interactively, or via
                   --interviewer/--respondent flags) which speaker label is
                   the interviewer and which is the respondent, so turns can
                   be tagged accordingly: interviewer turns are DISPLAYED for
                   context but excluded from coding and topic modeling
                   (speaker_role="interviewer"); respondent turns are fully
                   codeable/trainable (speaker_role="subject"); turns you
                   mark "other" (or leave unlabeled turns as "other") are
                   shown with a distinct badge and included in neither
                   exclusion -- decide those case-by-case in the Coding tab.

  .json         -- an already-structured canonical file (e.g. an LLM's
                   output using the prompt template in IMPORTING_DATA.md, for
                   a transcript that doesn't match either format above).
                   Validated and registered directly, no parsing.

Usage:
    python scripts/import_interview_transcript.py transcript.docx
    python scripts/import_interview_transcript.py transcript.docx \\
        --interviewer "Speaker 1" --respondent "Speaker 2" \\
        --person-name "Mark Lovell" --group-name "D&D players" \\
        --item-title "TTRPG history interview" --publish-date 2024-03-01

    python scripts/import_interview_transcript.py llm_output.json
"""
import argparse
import hashlib
import io
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import query_api
from paths import QUERIES_DIR

TIMESTAMP_RE = re.compile(r"^(\d{1,2}:\d{2}:\d{2})\s*(.*)$")


def extract_lines_from_bytes(raw_bytes, suffix):
    """Plain text lines, one per paragraph/line, in document order. `suffix`
    (e.g. ".docx") chooses the parser -- shared by extract_lines() (CLI, reads
    from disk) and viewer_server.py's import endpoints (already has the bytes
    from an uploaded file, no path on disk to read)."""
    if suffix.lower() == ".docx":
        import docx
        doc = docx.Document(io.BytesIO(raw_bytes))
        return [p.text for p in doc.paragraphs]
    return raw_bytes.decode("utf-8").splitlines()


def extract_lines(path):
    return extract_lines_from_bytes(path.read_bytes(), path.suffix.lower())


def parse_turns(lines):
    """Parses "HH:MM:SS[ Speaker Label]" + content lines into raw turns:
    [{timestamp, speaker_label: str|None, content}]. Skips any preamble
    before the first timestamp line (Word's "Audio file"/hyperlink/
    "Transcript" header, if present) and blank lines. A timestamp line with
    no speaker label (a brief interjection Word couldn't attribute) still
    starts its own turn, with speaker_label=None."""
    turns = []
    current = None
    for line in lines:
        stripped = line.strip()
        m = TIMESTAMP_RE.match(stripped)
        if m:
            if current is not None:
                turns.append(current)
            current = {"timestamp": m.group(1), "speaker_label": m.group(2).strip() or None, "content": ""}
        elif current is not None and stripped:
            current["content"] = f"{current['content']} {stripped}".strip()
    if current is not None:
        turns.append(current)
    return [t for t in turns if t["content"]]


# Matches a "Speaker: text" line, optionally markdown-bold-wrapped around the label and/or
# the colon ("**Speaker:** text" or "**Speaker**: text") -- see parse_labeled_turns() below.
# The label group is non-greedy and capped at 40 chars so it can't run away across a whole
# line looking for some unrelated colon later in the sentence.
LABELED_TURN_RE = re.compile(r"^\*{0,2}([A-Za-z][\w .'\-]{0,39}?)\*{0,2}\s*:\*{0,2}\s*(.*)$")

LABEL_MAX_WORDS = 5
LABEL_MIN_OCCURRENCES = 2


def parse_labeled_turns(lines, min_occurrences=LABEL_MIN_OCCURRENCES, max_label_words=LABEL_MAX_WORDS):
    """Parses a "Speaker: text" (or "**Speaker:** text") transcript -- no
    timestamps, one turn per line/paragraph, covering hand-typed
    transcripts, diarization-tool exports, and ChatGPT/Claude's own default
    "format this as an interview" output alike (see IMPORTING_DATA.md).

    Two passes, mirroring parse_turns()'s own shape:

    1. Candidate pass -- match LABELED_TURN_RE against every line, discard
       any candidate whose label is more than max_label_words words (a
       sentence that happens to contain a colon, e.g. "For example: ...",
       isn't a speaker cue), then keep only labels seen >= min_occurrences
       times. This recurrence check is the whole false-positive guard: a
       genuine speaker label repeats throughout the document; an incidental
       colon in the body text doesn't. Requires >= 2 distinct confirmed
       labels for the file to qualify as this format at all -- a single
       recurring prefix (e.g. every line starting "Note: ...") isn't a
       multi-speaker transcript.
    2. Turn-building pass -- a line matching a *confirmed* label starts a
       new turn (content = whatever followed the colon, possibly empty for
       a label-only line, with its content on the next paragraph(s)). Any
       other non-blank line -- including a match against an *unconfirmed*
       label -- is appended to the current turn's content, so a turn's text
       wrapped across multiple paragraphs is preserved as one turn. Lines
       before the first confirmed label are skipped (preamble), same as
       parse_turns().

    Returns [] (not an error) if fewer than 2 labels are confirmed -- same
    "not this format" contract parse_turns() has, so callers can try one
    parser then fall back to the other (see parse_transcript_turns())."""
    candidates = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        m = LABELED_TURN_RE.match(stripped)
        if m:
            label = m.group(1).strip()
            if label and len(label.split()) <= max_label_words:
                candidates.append((label, m.group(2).strip()))
                continue
        candidates.append((None, stripped))

    label_counts = {}
    for label, _ in candidates:
        if label:
            label_counts[label] = label_counts.get(label, 0) + 1
    confirmed = {label for label, count in label_counts.items() if count >= min_occurrences}
    if len(confirmed) < 2:
        return []

    turns = []
    current = None
    for label, text in candidates:
        if label in confirmed:
            if current is not None:
                turns.append(current)
            current = {"timestamp": None, "speaker_label": label, "content": text}
        elif current is not None:
            current["content"] = f"{current['content']} {text}".strip()
    if current is not None:
        turns.append(current)
    return [t for t in turns if t["content"]]


def parse_transcript_turns(lines):
    """Tries each supported transcript format in turn and returns
    (turns, format_name) -- format_name is "timestamped" or "labeled",
    whichever matched, or turns == [] and format_name is None if neither
    did. Shared by the CLI's import_docx_or_txt() and viewer_server.py's
    /api/import/transcript/parse, so the two never drift on which formats
    are recognized. See IMPORTING_DATA.md for what each format looks like."""
    turns = parse_turns(lines)
    if turns:
        return turns, "timestamped"
    turns = parse_labeled_turns(lines)
    if turns:
        return turns, "labeled"
    return [], None


BACKCHANNEL_MAX_WORDS = 8


def _turn_word_count(content):
    return len(content.split())


def _is_bridgeable_interjection(content, max_words):
    """A candidate backchannel must be short AND not a question -- a short
    turn ending in "?" ("What about session 0?", "What do you mean by
    that?") is a genuine follow-up that changes the topic, not an
    acknowledgement, and bridging across it would silently splice two
    unrelated answers together."""
    return _turn_word_count(content) <= max_words and not content.rstrip().endswith("?")


def bridge_backchannels(turns, max_words=BACKCHANNEL_MAX_WORDS):
    """Word's Transcribe (and similar tools) give every pause-detected
    fragment its own timestamped turn, so a listener's backchannel
    ("Right.", "Yeah.", "Hmm.") interjected mid-sentence splits the speaker
    who's actually talking into two disconnected halves -- bad for coding
    and worse for embedding. Two phases:

    1. Unconditionally merge every run of literally-consecutive same-speaker
       turns into one block -- no length cap, unlike segmentation.py's own
       merge pass, since here we want the *complete* passage, and
       segmentation.py's later sentence-aware splitting handles cutting it
       back down to size.
    2. Bridge short, non-question block(s) that are sandwiched between two
       blocks from the same (different) speaker -- e.g. a one-line
       backchannel from the interviewer in the middle of the subject's
       answer. The flanking same-speaker blocks are merged into one
       continuous passage; the bridged block(s) keep existing as their own
       turn(s), just repositioned to sit right after the merged passage
       instead of splitting it in two. A short block ending in "?" is never
       treated as bridgeable ("What about session 0?" is a real follow-up
       that changes the topic, not an acknowledgement) -- nor is a run of
       short blocks with no same-speaker block on both sides of it (a real
       interruption, or simply the transcript's opening/closing turn).
       Nothing is ever dropped, and no two different speakers' words are
       ever concatenated into one turn's content.
    3. Repositioning a bridged interjection in phase 2 can leave it
       immediately next to a *later*, non-bridgeable turn from that same
       interjecting speaker (the flanking merge in phase 2 only looks at
       what's already in `result`, not at what comes next in `blocks`) --
       a short "Right, right." interjection bridged out of the middle of
       one answer, then immediately followed by that same speaker's next
       real question, is a common shape and would otherwise surface as two
       separate same-speaker turns back to back. A final pass merges any
       turns left adjacent with the same speaker, so the output never has
       two consecutive turns from one speaker.

    Runs on parse_turns()'s raw output, before assign_roles() -- purely
    mechanical (turn length/shape + adjacency), no interviewer/subject role
    needed."""
    blocks = []
    for t in turns:
        content = (t["content"] or "").strip()
        if not content:
            continue
        if blocks and blocks[-1]["speaker_label"] == t["speaker_label"]:
            blocks[-1]["content"] = f"{blocks[-1]['content']} {content}".strip()
        else:
            blocks.append({**t, "content": content})

    result = []
    i, n = 0, len(blocks)
    while i < n:
        if not _is_bridgeable_interjection(blocks[i]["content"], max_words):
            result.append(blocks[i])
            i += 1
            continue
        j = i
        while j < n and _is_bridgeable_interjection(blocks[j]["content"], max_words):
            j += 1
        if result and j < n and result[-1]["speaker_label"] == blocks[j]["speaker_label"]:
            result[-1]["content"] = f"{result[-1]['content']} {blocks[j]['content']}".strip()
            result.extend(blocks[i:j])
            i = j + 1
        else:
            result.extend(blocks[i:j])
            i = j

    merged = []
    for t in result:
        if merged and merged[-1]["speaker_label"] == t["speaker_label"]:
            merged[-1]["content"] = f"{merged[-1]['content']} {t['content']}".strip()
        else:
            merged.append(dict(t))
    return merged


def prompt_for_roles(labels, unlabeled_count):
    """Interactive fallback when --interviewer/--respondent weren't given.
    Returns (label_roles: {label: "interviewer"|"subject"}, unlabeled_role)."""
    print(f"\nFound {len(labels)} speaker label(s)"
          + (f", plus {unlabeled_count} unlabeled turn(s)" if unlabeled_count else "") + ":")
    label_roles = {}
    for label in labels:
        while True:
            ans = input(f'  "{label}" is the [i]nterviewer, [r]espondent, or [o]ther/exclude? ').strip().lower()
            if ans in ("i", "interviewer"):
                label_roles[label] = "interviewer"
                break
            if ans in ("r", "respondent"):
                label_roles[label] = "subject"
                break
            if ans in ("o", "other", ""):
                break
            print("    please answer i, r, or o")
    unlabeled_role = "other"
    if unlabeled_count:
        ans = input(
            f"  How should the {unlabeled_count} unlabeled turn(s) be treated? "
            f"[i]nterviewer, [r]espondent, or [o]ther/exclude (default: o)? "
        ).strip().lower()
        if ans in ("i", "interviewer"):
            unlabeled_role = "interviewer"
        elif ans in ("r", "respondent"):
            unlabeled_role = "subject"
    return label_roles, unlabeled_role


def assign_roles(turns, label_roles, unlabeled_role):
    for t in turns:
        label = t["speaker_label"]
        t["speaker_role"] = unlabeled_role if label is None else label_roles.get(label, "other")
    return turns


def build_transcript_text(turns):
    lines = [f"[{t.get('speaker_name') or 'unknown'}] {t['content'].strip()}" for t in turns if t.get("content")]
    return "\n".join(lines)


def deterministic_item_id(group_name, person_name, item_title, publish_date):
    """A stable id derived from the record's own metadata, so re-running this
    import on unchanged input reproduces the same item_id -- and therefore the
    same segment_ids once segmented (see segmentation.py's hard rule on
    segment_id stability)."""
    key = "|".join(str(x or "") for x in (group_name, person_name, item_title, publish_date))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def prompt_optional(text):
    return input(text).strip() or None


def build_record(turns, *, group_name, person_name, person_title, item_title, publish_date, item_id=None):
    """Assemble one canonical record from role-assigned turns (see parse_turns/
    assign_roles) plus interview-level metadata. Shared by the CLI's
    import_docx_or_txt() and viewer_server.py's /api/import/transcript/commit,
    so the two never drift on the turns -> record mapping."""
    item_id = item_id or deterministic_item_id(group_name, person_name, item_title, publish_date)
    canonical_turns = [
        {
            "speaker_name": t["speaker_label"] or "Unknown",
            "speaker_id": None,
            "speaker_role": t["speaker_role"],
            "depth": 0,
            "timestamp": t["timestamp"],
            "content": t["content"],
        }
        for t in turns
    ]
    return {
        "group_name": group_name, "person_name": person_name, "person_title": person_title,
        "person_id": None, "item_id": item_id, "item_title": item_title, "source_url": None,
        "publish_date": publish_date, "duration_secs": None, "view_count": None,
        "like_count": None, "combined_classifier_score": None, "source_name": None,
        "transcript_text": build_transcript_text(canonical_turns), "turns": canonical_turns,
    }


def import_docx_or_txt(path, args):
    lines = extract_lines(path)
    turns, format_name = parse_transcript_turns(lines)
    if not turns:
        print("No turns found -- is this a Word Transcribe-style transcript (\"HH:MM:SS "
              "Speaker N\" lines) or a speaker-labeled transcript (\"Speaker: text\" lines, "
              "hand-typed or from ChatGPT/Claude)? See IMPORTING_DATA.md for examples of both. "
              "If your transcript is neither, use IMPORTING_DATA.md's LLM prompt template to "
              "convert it to canonical JSON instead, then run this script on that .json file.")
        sys.exit(1)
    turns = bridge_backchannels(turns)

    labels = sorted({t["speaker_label"] for t in turns if t["speaker_label"]})
    unlabeled_count = sum(1 for t in turns if t["speaker_label"] is None)

    if args.interviewer or args.respondent:
        label_roles = {l: "interviewer" for l in args.interviewer}
        label_roles.update({l: "subject" for l in args.respondent})
        unlabeled_role = "other"
    else:
        label_roles, unlabeled_role = prompt_for_roles(labels, unlabeled_count)
    assign_roles(turns, label_roles, unlabeled_role)

    group_name = args.group_name if args.group_name is not None else prompt_optional(
        "Group/organization this interview is about (optional): ")
    person_name = args.person_name or input("Interview subject's name: ").strip()
    if not person_name:
        print("A subject name is required (--person-name, or answer the prompt).")
        sys.exit(1)
    person_title = args.person_title if args.person_title is not None else prompt_optional(
        "Subject's title/role (optional): ")
    item_title = args.item_title or prompt_optional("Short title for this interview (optional): ") or path.stem
    publish_date = args.publish_date if args.publish_date is not None else prompt_optional(
        "Interview date, YYYY-MM-DD (optional): ")

    record = build_record(
        turns, group_name=group_name, person_name=person_name, person_title=person_title,
        item_title=item_title, publish_date=publish_date, item_id=args.item_id,
    )

    n_subject = sum(1 for t in turns if t["speaker_role"] == "subject")
    n_interviewer = sum(1 for t in turns if t["speaker_role"] == "interviewer")
    n_other = sum(1 for t in turns if t["speaker_role"] == "other")
    format_label = "timestamped" if format_name == "timestamped" else "speaker-labeled, no timestamp"
    print(f"\nDetected format: {format_label}. Parsed {len(turns)} turns: {n_subject} subject, "
          f"{n_interviewer} interviewer, {n_other} other/unresolved.")

    return [record], item_title


def normalize_json_records(records):
    """Fills in a derived item_id/transcript_text for any record that lacks
    one -- shared by the CLI's import_json() (reads records from a file) and
    viewer_server.py's /api/import/transcript/parse (already has the parsed
    JSON body, no file on disk)."""
    for rec in records:
        if not rec.get("item_id"):
            rec["item_id"] = deterministic_item_id(
                rec.get("group_name"), rec.get("person_name"), rec.get("item_title"), rec.get("publish_date"))
        else:
            rec["item_id"] = str(rec["item_id"])
        if not rec.get("transcript_text"):
            rec["transcript_text"] = build_transcript_text(rec.get("turns") or [])
    return records


def import_json(path):
    with open(path, encoding="utf-8") as f:
        records = json.load(f)
    query_api.validate_dataset_records(records, path.name, require_item_id=False)
    records = normalize_json_records(records)
    label = records[0].get("item_title") if records else path.stem
    return records, (label or path.stem)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help=".docx/.txt transcript, or a .json file already in canonical shape")
    parser.add_argument("--interviewer", action="append", default=[],
                         help="speaker label that is the interviewer (repeatable)")
    parser.add_argument("--respondent", action="append", default=[],
                         help="speaker label that is the respondent (repeatable)")
    parser.add_argument("--group-name", help="the organization/community this interview is about")
    parser.add_argument("--person-name", help="the interview subject's name")
    parser.add_argument("--person-title", help="the subject's job title/role")
    parser.add_argument("--item-title", help="a short title for this interview")
    parser.add_argument("--publish-date", help="YYYY-MM-DD")
    parser.add_argument("--item-id", help="override the deterministically-generated item_id")
    parser.add_argument("--append-to", metavar="DATASET_ID",
                         help="add this interview to an existing dataset (must be a previous "
                              "interview_import) instead of creating a new one-interview dataset -- "
                              "see queries/_index.json for existing dataset ids")
    args = parser.parse_args()

    path = Path(args.input)
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    if path.suffix.lower() == ".json":
        records, label = import_json(path)
    else:
        records, label = import_docx_or_txt(path, args)

    query_api.validate_dataset_records(records, path.name, require_item_id=True)

    if args.append_to:
        try:
            _, entry = query_api.append_to_dataset(QUERIES_DIR, args.append_to, records)
        except KeyError as exc:
            print(exc)
            sys.exit(1)
        except query_api.DatasetValidationError as exc:
            print(exc)
            sys.exit(1)
        print(f"\nAdded {len(records)} record(s) to '{entry['label']}' ({entry['json_file']}), "
              f"now {entry['count']} interview(s) total.")
        print("Run scripts/build_segments.py next to make the new interview(s) codeable.")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base_name = f"interview_{query_api.slugify(label)}_{stamp}"
    json_path, csv_path = query_api.write_export(QUERIES_DIR, base_name, records)
    query_api.register_export(
        QUERIES_DIR,
        dataset_id=f"q_{uuid.uuid4().hex[:10]}",
        label=f"{label} — {len(records)} interview(s)",
        kind="interview_import",
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
