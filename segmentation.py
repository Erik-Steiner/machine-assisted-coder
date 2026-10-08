"""Turns one item's `turns` list into stable-ID'd coding segments: merge
short same-speaker fragments forward, split long turns at sentence
boundaries, target ~120 words per segment. Source-agnostic -- works the same
whether `turns` came from an interview transcript, a Reddit thread, or any
other importer, as long as each turn has `content` (see docs/IMPORTING_DATA.md
for the canonical shape).

Calibrated against a measured sample of the OpenAI export (38,452 raw
utterances: median 51 words, mean 96, 12.7% over 200 words).

Do not change TARGET_WORDS / MERGE_MIN / SPLIT_MAX once real coding has
started: segment_id is derived from seg_index, so re-segmenting reshuffles
ids and orphans existing codes. Treat a later change as a deliberate
re-segmentation + migration, not a routine tweak.
"""
import re

TARGET_WORDS = 120
MERGE_MIN = 20
SPLIT_MAX = 220

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _word_count(text):
    return len(text.split())


def _split_long(text):
    """Split text into ~TARGET_WORDS chunks without breaking mid-sentence."""
    sentences = [s for s in _SENTENCE_SPLIT.split(text.strip()) if s]
    if not sentences:
        return [text]
    chunks = []
    current = []
    current_words = 0
    for sent in sentences:
        w = _word_count(sent)
        if current and current_words + w > TARGET_WORDS:
            chunks.append(" ".join(current))
            current = []
            current_words = 0
        current.append(sent)
        current_words += w
    if current:
        chunks.append(" ".join(current))
    return chunks


def _segment_pieces(record):
    """The merge/split algorithm itself, as (merged_turn, piece_text) pairs in
    seg_index order. Each merged_turn carries `turn_indices`: the 0-based
    positions in record["turns"] it was built from.

    Both public functions below read from this one loop, so the segment texts
    and the turn indices behind them can't drift apart. Indices count every
    turn in the original list, including empty-content ones the loop skips,
    so they line up with anything else that numbers record["turns"] directly.
    """
    utterances = record.get("turns") or []

    # Pass 1: merge a short utterance forward into the next same-speaker one,
    # so caption-artifact fragments ("Right.") don't become their own segment
    # unless there's no same-speaker neighbor to merge into.
    merged = []
    for turn_index, u in enumerate(utterances):
        content = (u.get("content") or "").strip()
        if not content:
            continue
        speaker = u.get("speaker_name")
        if (merged and merged[-1]["speaker_name"] == speaker
                and _word_count(merged[-1]["content"]) < MERGE_MIN):
            merged[-1]["content"] = f"{merged[-1]['content']} {content}"
            merged[-1]["turn_indices"].append(turn_index)
        else:
            merged.append({
                "speaker_name": speaker,
                "speaker_id": u.get("speaker_id"),
                "speaker_role": u.get("speaker_role"),
                "depth": u.get("depth"),
                "timestamp": u.get("timestamp"),
                "content": content,
                "turn_indices": [turn_index],
            })

    # Pass 2: split turns that are still too long.
    out = []
    for u in merged:
        wc = _word_count(u["content"])
        pieces = _split_long(u["content"]) if wc > SPLIT_MAX else [u["content"]]
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            out.append((u, piece))
    return out


def segment_interview(record):
    """record: one row from queries/*.json (needs item_id and turns;
    group_name/person_name are copied through if present). Returns a list of
    segment dicts (without dataset_id -- the caller, e.g.
    scripts/build_segments.py, knows which dataset it's processing and adds
    that field before storing)."""
    item_id = record.get("item_id")
    segments = []
    for seg_index, (u, piece) in enumerate(_segment_pieces(record)):
        segments.append({
            "segment_id": f"{item_id}:{seg_index:04d}",
            "item_id": item_id,
            "group_name": record.get("group_name"),
            "person_name": record.get("person_name"),
            "seg_index": seg_index,
            "speaker_name": u["speaker_name"],
            "speaker_id": u["speaker_id"],
            "speaker_role": u["speaker_role"],
            "depth": u["depth"],
            "timestamp": u["timestamp"],
            "text": piece,
            "word_count": _word_count(piece),
        })
    return segments


def segment_turn_indices(record):
    """For each segment segment_interview(record) returns, the 0-based indices
    into record["turns"] of the turns it came from -- a list of lists in the
    same order, so result[k] belongs to seg_index k.

    The `segments` table doesn't store this, but external pipelines key on
    source turns (e.g. scripts/export_interview_turns.py's turn CSVs, whose
    turn_id is index + 1). A merged segment lists several indices; a long turn
    split into several segments repeats its index in each piece. Indices are
    positions in the original list, so empty-content turns (never part of a
    segment) still count. Pure: no DB, no I/O.
    """
    return [list(u["turn_indices"]) for u, _ in _segment_pieces(record)]
