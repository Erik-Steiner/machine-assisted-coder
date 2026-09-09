"""Regression tests for scripts/import_interview_transcript.py's line->turns
parsing -- parse_turns()/parse_labeled_turns()/parse_transcript_turns() and
bridge_backchannels() are pure functions over plain lists/dicts (no DB, no
server, no file I/O needed to exercise them), which is what makes them cheap
to unit-test even though this repo otherwise has no automated test suite
(see CLAUDE.md's "no automated test suite" hard rule -- this file exists
because the researcher asked for a regression net around the labeled-turn
format's recurrence-threshold heuristic specifically, not as a step toward
a general suite).

Fixtures here are small hand-written examples, not the real sample files
under data examples/ or interview example/ -- those are real, researcher-
provided or gitignored-as-scratch, and not guaranteed to exist in every
checkout (data examples/ is explicitly gitignored as "not meant to be
redistributed"). Keeping fixtures inline keeps these tests hermetic and
portable.

Run with: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "scripts"))

import import_interview_transcript as imp  # noqa: E402


class ParseLabeledTurnsTests(unittest.TestCase):
    def test_plain_colon_labels(self):
        lines = [
            "Laura: So thanks for making time today.",
            "Jordan: Of course, happy to help.",
            "Laura: Let's start with your background.",
            "Jordan: Sure, I've been in this field ten years.",
        ]
        turns, fmt = imp.parse_transcript_turns(lines)
        self.assertEqual(fmt, "labeled")
        self.assertEqual([t["speaker_label"] for t in turns], ["Laura", "Jordan", "Laura", "Jordan"])

    def test_markdown_bold_labels_both_asterisk_placements(self):
        lines = [
            "**Interviewer:** So thanks for making time today.",
            "**Jordan:** About three years now.",
            "**Interviewer:** And was that a relief?",
            "**Jordan**: Both, honestly.",
        ]
        turns, fmt = imp.parse_transcript_turns(lines)
        self.assertEqual(fmt, "labeled")
        labels = {t["speaker_label"] for t in turns}
        self.assertEqual(labels, {"Interviewer", "Jordan"})
        for t in turns:
            self.assertNotIn("*", t["speaker_label"])
            self.assertNotIn("*", t["content"])

    def test_interjection_bridged_and_consolidated(self):
        # Mirrors data examples/Interview Self-transcribed.docx's own shape: a
        # short, non-question interjection ("L: Right, right.") sandwiched
        # inside X's answer, immediately followed by that same interjecting
        # speaker's next real turn -- the case bridge_backchannels()'s third
        # (consolidation) phase was added for. See DEVELOPMENT.md.
        lines = [
            "L: And was that a relief or was it kind of unsettling?",
            "X: Both, honestly. At first it was a relief, but then—",
            "L: Right, right.",
            "X: —but then you kind of realize you missed the small talk more than you expected.",
            "L: Can you say more about that?",
        ]
        turns, fmt = imp.parse_transcript_turns(lines)
        self.assertEqual(fmt, "labeled")
        bridged = imp.bridge_backchannels(turns)

        for a, b in zip(bridged, bridged[1:]):
            self.assertNotEqual(a["speaker_label"], b["speaker_label"],
                                 "no two consecutive turns should share a speaker after bridging")

        orig_words = sum(len(t["content"].split()) for t in turns)
        bridged_words = sum(len(t["content"].split()) for t in bridged)
        self.assertEqual(orig_words, bridged_words, "bridging must never drop or add words")

    def test_one_off_colon_is_not_mistaken_for_a_speaker(self):
        lines = [
            "Laura: So thanks for making time today.",
            "Note: this interview was conducted over Zoom.",
            "Jordan: No problem at all, happy to help.",
            "Laura: Great, let's start with your background.",
            "Jordan: Sure, ten years in this field.",
        ]
        turns, fmt = imp.parse_transcript_turns(lines)
        self.assertEqual(fmt, "labeled")
        labels = {t["speaker_label"] for t in turns}
        self.assertEqual(labels, {"Laura", "Jordan"})
        self.assertIn("this interview was conducted over Zoom", turns[0]["content"])

    def test_unsupported_prose_produces_no_turns(self):
        lines = [
            "This is a summary of the interview rather than a verbatim transcript.",
            "The subject discussed their career history and views on remote work.",
        ]
        turns, fmt = imp.parse_transcript_turns(lines)
        self.assertEqual(turns, [])
        self.assertIsNone(fmt)


class ParseTimestampedTurnsRegressionTests(unittest.TestCase):
    def test_timestamped_format_still_takes_priority(self):
        lines = [
            "00:00:04 Speaker 1",
            "So thanks for making time today.",
            "00:00:08 Speaker 2",
            "Of course, happy to help.",
        ]
        turns, fmt = imp.parse_transcript_turns(lines)
        self.assertEqual(fmt, "timestamped")
        self.assertEqual([t["speaker_label"] for t in turns], ["Speaker 1", "Speaker 2"])


if __name__ == "__main__":
    unittest.main()
