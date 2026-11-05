"""Shared inputs and locations for the LLM topic-coding pipeline (scripts/llm_topics/).

The pipeline codes executive answer turns from exports/interview_turns/ with a
seeded topic codebook: the five researcher themes in exports/topic_seed_pack.json
stay locked (deductive codes), and emergent topics are added around them
(inductive codes). Every stage reads exports/ only and writes under
exports/llm_topics/ -- it never opens coding.db or queries/, so it can't touch
the researcher's codes (CLAUDE.md rules 6 and 11).

Read the four per-company turn files, never all_companies_interview_turns.csv:
that file was edited after export (324 columns, 64,067 rows vs the documented
64,061) and no longer matches its README.
"""
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
import paths  # noqa: E402

EXPORTS_DIR = paths.EXPORTS_DIR
TURNS_DIR = EXPORTS_DIR / "interview_turns"
SEED_PACK = EXPORTS_DIR / "topic_seed_pack.json"
SEGMENT_SCORES = EXPORTS_DIR / "segment_topic_scores.csv"
OUT_DIR = EXPORTS_DIR / "llm_topics"
STAGE0_DIR = OUT_DIR / "stage0"
STAGE1_DIR = OUT_DIR / "stage1"
STAGE2_DIR = OUT_DIR / "stage2"

# Discovery and codebook consolidation are the most judgment-heavy steps and run on
# a ~1,500-turn sample, so they use a stronger model; bulk coding of all ~26k turns
# uses Haiku. Researcher's choice, 2026-10-06.
DISCOVERY_MODEL = "claude-sonnet-5-5"
CODING_MODEL = "claude-haiku-4-5"

# What the corpus is and why it's being coded -- shown to every model call. Written
# by the pipeline's author from the theme definitions; the researcher should check
# it says what the study is actually about, since it steers what counts as a topic.
STUDY_CONTEXT = """\
The corpus is public interviews, talks and panels with executives of four frontier AI \
firms -- OpenAI, Anthropic, Google and Google DeepMind -- transcribed from YouTube, \
roughly 2016-2026. It is coded for a qualitative study of how these executives talk \
about developing, positioning and shaping AI and the market around it. The unit of \
coding is one executive answer turn (what the executive says in reply to one question)."""

COMPANY_FILES = ["OpenAI", "Anthropic", "Google", "DeepMind"]

# A judged interview counts as read in full -- so its uncoded segments are
# verified negatives -- only with at least this many coded segments. Below it,
# the "fully coded" mark most likely came from the bulk backfill (any interview
# with one code was marked), so its uncoded segments are neutral, not negative.
# Researcher's rule, 2026-10-06.
READ_IN_FULL_MIN_CODED = 5

# Executive answer turns shorter than this are tagged non_substantive by rule
# ("Yeah.", "Exactly, right.") rather than sent to the model.
MIN_CODED_WORDS = 5


def load_turns():
    """All turns from the four per-company exports, in spoken order."""
    frames = [pd.read_csv(TURNS_DIR / f"{name}_interview_turns.csv", encoding="utf-8-sig",
                          low_memory=False, dtype={"comment_id": str})
              for name in COMPANY_FILES]
    turns = pd.concat(frames, ignore_index=True).sort_values(["interview_id", "turn_id"])
    return turns.reset_index(drop=True)


def load_seed_pack():
    with open(SEED_PACK, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
