"""Regression tests for segmentation.py's segment_turn_indices() and the shared
merge/split loop behind it and segment_interview().

Why this qualifies under CLAUDE.md rule 8's narrow exception: both are pure
functions (a record dict in, lists out; no DB, server or file I/O), and the
cost of a silent mistake is high. segment_id is derived from seg_index, so any
change to segment_interview()'s output orphans every existing code (rule 4),
and a wrong turn index quietly mislabels turn-keyed exports downstream.

The "unchanged" checks compare against a frozen copy of the pre-K6
segment_interview() embedded below, so a later edit to the shared loop that
alters segmentation fails here rather than in the researcher's coding.db.

Run with: python -m unittest discover -s tests
"""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import segmentation  # noqa: E402
from segmentation import segment_interview, segment_turn_indices  # noqa: E402


def _legacy_segment_interview(record):
    """segment_interview() as it was before K6 refactored it onto a shared
    helper. Frozen copy: do not "fix" or update it alongside segmentation.py."""
    word_count = segmentation._word_count
    item_id = record.get("item_id")
    utterances = record.get("turns") or []
    merged = []
    for u in utterances:
        content = (u.get("content") or "").strip()
        if not content:
            continue
        speaker = u.get("speaker_name")
        if (merged and merged[-1]["speaker_name"] == speaker
                and word_count(merged[-1]["content"]) < 20):
            merged[-1]["content"] = f"{merged[-1]['content']} {content}"
        else:
            merged.append({
                "speaker_name": speaker,
                "speaker_id": u.get("speaker_id"),
                "speaker_role": u.get("speaker_role"),
                "depth": u.get("depth"),
                "timestamp": u.get("timestamp"),
                "content": content,
            })
    segments = []
    seg_index = 0
    for u in merged:
        wc = word_count(u["content"])
        pieces = segmentation._split_long(u["content"]) if wc > 220 else [u["content"]]
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
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
                "word_count": word_count(piece),
            })
            seg_index += 1
    return segments


def _words(n, tag="w"):
    return " ".join(f"{tag}{i}" for i in range(n))


def _sentences(n_sentences, words_each=15):
    return " ".join(_words(words_each, f"s{k}x") + "." for k in range(n_sentences))


def _turn(speaker, content, **extra):
    return {"speaker_name": speaker, "content": content, **extra}


class SegmentTurnIndicesTest(unittest.TestCase):
    def test_constants_unchanged(self):
        # Rule 4: changing these reshuffles segment_ids and orphans codes.
        self.assertEqual((segmentation.TARGET_WORDS, segmentation.MERGE_MIN, segmentation.SPLIT_MAX),
                         (120, 20, 220))

    def test_short_same_speaker_turns_merge(self):
        rec = {"item_id": "a", "turns": [
            _turn("Q", "Right."),
            _turn("Q", "So tell me about it."),
            _turn("A", _words(30)),
        ]}
        segs = segment_interview(rec)
        self.assertEqual([s["text"] for s in segs], ["Right. So tell me about it.", _words(30)])
        self.assertEqual(segment_turn_indices(rec), [[0, 1], [2]])

    def test_long_turn_split_repeats_index(self):
        rec = {"item_id": "b", "turns": [
            _turn("Q", _words(25)),
            _turn("A", _sentences(20)),  # 300 words > SPLIT_MAX
        ]}
        segs = segment_interview(rec)
        indices = segment_turn_indices(rec)
        self.assertGreater(len(segs), 2)
        self.assertEqual(indices, [[0]] + [[1]] * (len(segs) - 1))

    def test_empty_turns_skipped_but_indices_are_original_positions(self):
        rec = {"item_id": "c", "turns": [
            _turn("Q", ""),
            _turn("Q", _words(25)),
            _turn("A", "   "),
            _turn("A", None),
            _turn("A", _words(30)),
            _turn("Q", "\n"),
        ]}
        segs = segment_interview(rec)
        self.assertEqual(len(segs), 2)
        self.assertEqual(segment_turn_indices(rec), [[1], [4]])

    def test_merge_across_an_empty_turn(self):
        rec = {"item_id": "d", "turns": [
            _turn("A", "Yes."),
            _turn("A", ""),
            _turn("A", "And more."),
        ]}
        self.assertEqual(segment_turn_indices(rec), [[0, 2]])

    def test_speaker_change_blocks_merge(self):
        rec = {"item_id": "e", "turns": [
            _turn("Q", "Why?"),
            _turn("A", "Because."),
            _turn("Q", "Okay."),
            _turn("Q", "Next."),
        ]}
        self.assertEqual(segment_turn_indices(rec), [[0], [1], [2, 3]])

    def test_merge_stops_once_long_enough(self):
        rec = {"item_id": "f", "turns": [
            _turn("A", _words(25)),  # >= MERGE_MIN: next same-speaker turn starts fresh
            _turn("A", "Short."),
        ]}
        self.assertEqual(segment_turn_indices(rec), [[0], [1]])

    def test_merged_then_split(self):
        rec = {"item_id": "g", "turns": [
            _turn("A", "Well."),
            _turn("A", _sentences(20)),
        ]}
        indices = segment_turn_indices(rec)
        self.assertGreater(len(indices), 1)
        self.assertTrue(all(ix == [0, 1] for ix in indices))

    def test_empty_record(self):
        self.assertEqual(segment_turn_indices({"item_id": "h"}), [])
        self.assertEqual(segment_turn_indices({"item_id": "h", "turns": None}), [])

    def test_returned_lists_are_independent(self):
        rec = {"item_id": "i", "turns": [_turn("A", _sentences(20))]}
        indices = segment_turn_indices(rec)
        indices[0].append(99)
        self.assertEqual(indices[1], [0])


class SegmentInterviewUnchangedTest(unittest.TestCase):
    def _random_record(self, rng, n):
        speakers = ["Q", "A", "B", None]
        turns = []
        for t in range(n):
            kind = rng.random()
            if kind < 0.1:
                content = rng.choice(["", "   ", None, "\n\t"])
            elif kind < 0.5:
                content = _words(rng.randint(1, 25), f"t{t}w")
            elif kind < 0.85:
                content = _sentences(rng.randint(1, 8), rng.randint(3, 20))
            else:
                content = _sentences(rng.randint(15, 40), rng.randint(5, 25))
            turns.append({
                "speaker_name": rng.choice(speakers),
                "speaker_id": rng.randint(1, 5),
                "speaker_role": rng.choice(["host", "guest", None]),
                "depth": rng.randint(0, 3),
                "timestamp": f"0:{t:02d}:00",
                "content": content,
            })
        return {"item_id": f"r{n}", "group_name": "G", "person_name": "P", "turns": turns}

    def test_matches_legacy_on_synthetic_records(self):
        rng = random.Random(6)
        for n in [0, 1, 2, 5, 20, 60, 150]:
            for _ in range(5):
                rec = self._random_record(rng, n)
                with self.subTest(n=n):
                    new = segment_interview(rec)
                    self.assertEqual(new, _legacy_segment_interview(rec))
                    self.assertEqual([list(s) for s in new],
                                     [list(s) for s in _legacy_segment_interview(rec)])  # key order
                    indices = segment_turn_indices(rec)
                    self.assertEqual(len(indices), len(new))
                    for seg, ix in zip(new, indices):
                        source = " ".join(" ".join(
                            (rec["turns"][i]["content"] or "").strip() for i in ix).split())
                        self.assertIn(" ".join(seg["text"].split()), source)
                        self.assertTrue(all(rec["turns"][i]["speaker_name"] == seg["speaker_name"] for i in ix))
                    self.assertEqual(indices, sorted(indices))


if __name__ == "__main__":
    unittest.main()
