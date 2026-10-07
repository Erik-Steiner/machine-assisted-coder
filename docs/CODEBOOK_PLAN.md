# Plan: codebook rigor, near misses, and the `codebook_topic_themes.json` export

Agreed 2026-10-07. This plan expands roadmap items **B4** (memos and codebook rigor) and
**D1** (scoring-pack export) into buildable work, items K1–K8 below. It is written for a
developer-agent who has not seen the conversation that produced it. Read `CLAUDE.md` first;
every hard rule there applies, especially 4 (segmentation constants), 5 (no hard deletes),
6 and 11 (never write through the app against a real project). If this doc and the code
disagree, the code wins (rule 1); fix this doc.

## Why this exists

On 2026-10-06 the researcher started an external pipeline (`scripts/llm_topics/`) that uses
the app's codebook to seed an LLM topic model of the interview turns. Getting a usable
codebook out of the app took a day of manual work, because the app does not hold most of
what an external model needs. What was missing, and how it was patched by hand:

| Needed by an external model | What the app had | Manual workaround |
|---|---|---|
| Definitions with inclusion and exclusion criteria | One free-text `themes.description`, usually one sentence | Researcher typed `include` / `exclude` / `boundary_notes` into a JSON file |
| Exemplars the researcher stands behind | `topic_seed_pack.json` `exemplars` = the model's top 20 predictions, mixing coded and uncoded passages; some uncoded "exemplars" were clearly off-theme | Filtered to `coded=True`, then picked 5 per theme by script, then the researcher swapped weak ones |
| Near misses (looks like the theme, isn't) | Nothing | Script listed reviewed negatives with the highest out-of-fold score; researcher marked each `near_miss` / `missed_positive` / `skip` in a CSV |
| Trustworthy negatives | "Fully coded" marks, but most came from the bulk backfill (any interview with one code was marked) | Researcher's rule: only interviews with 5 or more coded segments count as read in full |
| Duplicates removed | Twin records of one video produce the same exemplar twice | Text-similarity dedupe in the script |
| A link from segments to source turns | None; segments merge and split turns | Re-ran `segmentation.py`'s rules to rebuild the map |

Evidence from the real export at that time (model `tfidf_v1_20261001_202611`):
- 42 of the 75 "fully coded" interviews had exactly one coded segment, against a median of 43
  segments per interview.
- Several of the highest-scoring reviewed negatives were plainly on-theme. For example, for
  safety_and_ethics: *"We wanted to take a forward-looking approach to thinking about safety
  of AI systems…"*
- Exemplars appeared twice when the same video was held as two records (for example, Sam
  Altman and Kevin Weil at one event).

The goal is that a researcher can do all of this inside the app while coding, and that one
export button produces a file an external model (LLM, embedding model, anything) can consume
without hand-editing.

## Terms

Use these words in code, UI and docs, and nothing else for the same idea.

| Term | Meaning |
|---|---|
| **Definition** | `themes.description`: what the theme is, in a few sentences. |
| **Inclusion / exclusion criteria** | Concrete rules: "code this when…", "don't code this when…". |
| **Boundary notes** | Free text on hard cases and on overlap with sibling themes. |
| **Exemplar** | A coded passage the researcher flags as a clear, typical case of the theme. Always an active code. |
| **Near miss** | A rejected passage the researcher flags as a useful contrast: it looks like the theme but isn't. Always an active explicit rejection. |
| **Missed positive** | A judged negative that turns out to be the theme. Not stored as such: accepting it writes a normal code, which cancels the rejection (A5 exclusivity). |
| **Read in full** | An interview the researcher read end to end against every theme in the scheme. Its uncoded segments are real negatives. |
| **Positives-only interview** | A judged interview that isn't read in full. Its codes are real positives; its uncoded segments are neutral, not negatives. |
| **Neutral** | Never examined for the theme. Not a negative. |
| **Twins** | Near-identical passages from different records (the same video held per executive, or re-uploads). |

## Work items

Build in this order: **K1 → K2 → K3 → K5 → K4 → K6 → K7 → K8**. K1 is the only schema change.
K2 and K3 give the researcher somewhere to enter the data. K5 must land before K4 and K7,
because both depend on knowing which interviews are read in full.

### K1 Schema (one version bump) — done 2026-10-07, schema v7

Add these columns in one `CURRENT_SCHEMA_VERSION` bump, following the existing idiom in
`coding_store/schema.py` `_migrate()`: check `PRAGMA table_info`, then `ALTER TABLE … ADD
COLUMN`. At the time of writing the version is 6, so this would be 7. Use whatever comes next
when you implement.

| Table.column | Type | Purpose |
|---|---|---|
| `themes.inclusion_criteria` | TEXT | K2 |
| `themes.exclusion_criteria` | TEXT | K2 |
| `themes.boundary_notes` | TEXT | K2 |
| `codes.is_exemplar` | INTEGER NOT NULL DEFAULT 0 | K3 |
| `codes.exemplar_note` | TEXT | K3: why it is typical |
| `negatives.is_near_miss` | INTEGER NOT NULL DEFAULT 0 | K3/K4 |
| `negatives.near_miss_note` | TEXT | K3/K4: what makes it not the theme |
| `item_reviews.read_in_full_confirmed_at` | TEXT | K5 |

Design decisions:
- **Flags on the existing rows, not a new examples table.** An exemplar must be an active
  code and a near miss must be an active rejection. A flag on that row enforces this for
  free. If the researcher later removes the code, or flips a near miss into a code, the row
  is soft-deleted (`deleted_at`) and the flag goes with it. A separate table would need its
  own consistency rule. This follows CLAUDE.md's "tag the codes row" convention.
- **Separate note columns, not `codes.note`.** `codes.note` already carries provenance text
  (`"review queue: top"` from `coding_store/decisions.py`). Overloading it would mix the
  two.
- `roadmap B4` listed `themes.inclusion_criteria`, `exclusion_criteria` and
  `codes.is_exemplar`. This plan adds the rest; update the roadmap's schema list.

Verification: run the migration on a **copy** of a real `coding.db` first (rule 4's
guidance), and confirm row counts in `themes`, `codes`, `negatives` and `item_reviews` are
unchanged.

### K2 Theme editor with prompts, and a completeness check — done 2026-10-07

**As built (researcher's decision, 2026-10-07):** the editor lives on a new **Codebook**
sub-page of the Train tab (`viewer_static/codebook.js`, a Models | Codebook switcher), not in
the Code tab's inline edit form, which still edits name and definition quickly. The Code tab's
theme rows carry a "Codebook n/5" badge that opens the theme on that page. Completeness is
`coding_store/codebook.py`'s `theme_completeness()`, with the thresholds below confirmed.
"Gold" counts read-in-full interviews as confirmed or interactive marks
(`codebook.read_in_full_items()`), the K5 rule, ahead of K5's UI. The original spec follows.

`viewer_static/coding.js` has the theme edit form (the `theme-edit-desc` textarea, around
the "What counts as this theme?" placeholder). Extend it to four fields: Definition,
Include when, Exclude when, Boundary notes. `coding_store/themes.py` `update_theme()` must
add the three new fields to its `allowed` set; `create_theme()` should accept them too. Both
already log to `activity_log`.

Prompting the researcher (the point of this item):
- Each field gets a placeholder with a worked example, not a generic hint. For example:
  - Include when: "cites a metric, benchmark, usage number or ranking to judge a model"
  - Exclude when: "a plain capability claim with no measure"
- A **completeness indicator** on each theme row in the codebook panel, e.g. "Codebook 3/5".
  Hovering or clicking lists what's missing.
- The same indicator in the K7 export dialog, and a one-line note on the Model tab's train
  banner when a theme being trained has no criteria. Advisory only: never block coding,
  training or export (same stance as roadmap C4).

Put the completeness rule in one pure function, e.g. `coding_store/codebook.py`
`theme_completeness(theme, counts)`, used by both the UI endpoint and the export. Starting
checks (thresholds are constants with a comment; confirm them with the researcher):

| Check | Passes when |
|---|---|
| Definition | `description` has at least 15 words |
| Criteria | inclusion and exclusion criteria both non-empty |
| Exemplars | at least 3 active exemplars, from at least 2 `group_name`s |
| Near misses | at least 2 active near misses |
| Gold | at least one read-in-full interview (K5) contains a positive for the theme |

### K3 Marking exemplars and near misses while coding

- **Coding tab:** a star toggle on each *coded* theme chip of a segment card marks or unmarks
  `is_exemplar`. Marking opens a one-line prompt for `exemplar_note` (optional, skippable).
  Near misses are not marked here: Coding has no rejection action.
- **Review tab, triage panel** (`viewer_static/review.js`; current keys are `a` / `r` / `s` /
  `u` / `j` / `k`):
  - `e` = accept and flag as exemplar
  - `m` = reject and flag as near miss
  
  Check both keymaps for collisions before choosing. The Coding tab uses `1`–`9`, `n`, `j`,
  `k` and the arrows. Show both keys in the panel's key legend.
- **Recording:** `coding_store/decisions.py` `record_decision()` must handle the flagged
  variants and its `undo()` must clear the flag. Two options:
  - add `exemplar` / `near_miss` to `DECISIONS`
  - keep `accept` / `reject` and record the flag in a new nullable column
  
  Pick one, and make sure C2's accept-rate statistics still count an exemplar as an accept
  and a near miss as a reject.
- **Codebook panel:** per theme, a collapsible list of its exemplars and near misses with
  their notes, an unflag button, and a link that opens the passage. The researcher must be
  able to review the whole set in one place before exporting.
- **Twin warning:** when flagging, warn if the passage has a near-identical twin already
  flagged. Two sources:
  - `duplicate_cluster_items` (from `scripts/find_duplicates.py`)
  - a text check: word-set Jaccard of 0.5 or more within the same `person_name`
  
  `scripts/llm_topics/stage0_prepare.py` `near_duplicate()` is a working reference.

### K5 Trustworthy "read in full"

The problem: `item_reviews` mixes two kinds of marks, and only one supports negatives.
- **Interactive marks** (`reviewed.set_item_review()`): the researcher clicked "Mark fully
  coded" after reading. Here `recorded_at == marked_at`.
- **Bulk backfill marks** (`reviewed.bulk_mark_items()`): every interview with any code was
  marked in one action, dated back to its last code. Here `recorded_at > marked_at`. Many of
  these interviews were never read in full.
- Marks made before schema v6 have `recorded_at IS NULL`, so their provenance is unknown.

Build:
1. A pure function, e.g. `reviewed.read_in_full_status(dataset_id, item_id, scheme_id)`,
   returning one of:
   - `confirmed`: `read_in_full_confirmed_at` is set
   - `interactive`: an interactive mark
   - `bulk_unconfirmed`: a bulk mark not yet confirmed
   - `unknown`: a pre-v6 mark
   - `none`: no active mark
   
   Report the researcher's fallback heuristic separately: 5 or more coded segments in the
   scheme (`inferred_by_coverage`). Never mix it into the status itself.
2. **"Confirm read in full"**: an action in the Coding tab's interview header for
   bulk-marked or unknown interviews. Also add a new list of those interviews to the Datasets
   panel, next to the existing bulk-mark tool (`#backfillPanel`, which only lists interviews
   *not yet* marked). It sets
   `read_in_full_confirmed_at`. It must **not** change `marked_at`, because `marked_at`
   decides which themes the mark covers (A6). Log it to `activity_log`.
3. A filter in the Coding tab's interview list, "Marked but not confirmed", so the
   researcher can work through them.

**Open decision for the researcher, not the developer:** should training's `reviewed`
negative mode (`labels.reviewed_negative_segment_ids()`) also stop counting unconfirmed bulk
marks? That changes what every model learns. Ship K5 with training unchanged, then ask. If
the answer is yes, add it as an option alongside `negative_mode` and log it per run, not as
a silent change.

### K4 Near-miss discovery queue

Add a queue mode, e.g. `boundary`, to `coding_store/queues.py` `QUEUE_MODES`. It lists judged
negatives for the theme, highest `oof_score` first:
- explicit rejections
- uncoded segments in **read-in-full** interviews (K5 status `confirmed` or `interactive`)
- "none of these" confirmations

Exclude passages already flagged as near misses. Its purpose is to surface two things the
researcher otherwise never sees: missed positives and good contrast examples.

How it differs from `suspect`: `suspect` mixes low-scoring codes with high-scoring
negatives, to find miscodes. `boundary` is negatives only, and its main output is near
misses.

In this mode the triage actions mean:
- `a`: it is the theme (a missed positive), so write a code
- `m`: reject and flag as a near miss
- `r`: plain rejection
- `s`: skip

Record every decision in `review_decisions` with `mode='boundary'`, as B2 does. Add the mode
to Review's mode selector with a one-line explanation, in the same style as the other modes.

### K6 Segment → turn linkage

External pipelines often key on source turns (for example, the turn CSVs from
`scripts/export_interview_turns.py`, where `turn_id` is the 1-based position in the record's
`turns` list). Segments merge short same-speaker turns and split long ones, and the
`segments` table doesn't store which turns it came from.

Add a pure function to `segmentation.py`, e.g. `segment_turn_indices(record)`. It returns, for
each segment, the 0-based indices of the turns it came from. Implement it by sharing one code
path with `segment_interview()`, so the two can't drift. **Don't change `TARGET_WORDS` /
`MERGE_MIN` / `SPLIT_MAX` (rule 4).** Reference implementation:
`scripts/llm_topics/stage0_prepare.py` `build_turn_segment_map()`. It is verified to
reproduce every scored `segment_id` on the real corpus. Once the function exists, make that
script import it.

Verify the `turn_id = index + 1` assumption against `export_interview_turns.py` before
relying on it: check whether that script skips empty-content turns while numbering.

### K7 The export: `codebook_topic_themes.json` and `codebook_labels.csv`

Build a new top-level, read-only module `codebook_export.py`, in the same style as
`topic_model_export.py` and `appendix_export.py`. The existing exports stay as they are
(additive-features rule):
- `/api/export/codebook` (the raw table dump)
- `topic_seed_pack` (keep it, but document in DEVELOPMENT.md that its `exemplars` are model
  predictions, not researcher-chosen)

Endpoint: `GET /api/export/codebook_themes?scheme_id=<id>`. It writes into `EXPORTS_DIR` like
the other export endpoints; add it to `viewer_server.py`'s module docstring endpoint list. A
button sits next to the existing codebook export in the Web Appendix tab
(`viewer_static/appendix.js`) and in the Coding tab's codebook panel. Clicking it shows each
theme's completeness (K2) first, with "Export anyway" always available. Log
`codebook_themes_export` to `activity_log` and add its label to `appendix.js`.

**`codebook_topic_themes.json`.** It is small, human-readable, and meant to be pasted into a
prompt or read by a script:

```json
{
  "format": "codebook_topic_themes",
  "format_version": 1,
  "generated_at": "2026-10-07T18:00:00+00:00",
  "schema_version": 7,
  "scheme": {"scheme_id": "default", "name": "...", "description": "..."},
  "codebook_version": "sha256:<hash of the canonical JSON of `topics`>",
  "label_policy": {
    "positive": "an active code on the theme or any of its sub-codes (roll-up, A2)",
    "negative": "an explicit rejection, a 'none of these' confirmation, or an uncoded segment in a read-in-full interview, judged after the theme was created",
    "neutral": "everything else; absent from codebook_labels.csv",
    "read_in_full_counts": ["confirmed", "interactive"],
    "inferred_by_coverage_min_coded": 5
  },
  "topics": [
    {
      "topic_id": "anthropomorphism",
      "name": "Anthropomorphism",
      "parent_topic_id": null,
      "status": "active",
      "definition": "...",
      "inclusion_criteria": "...",
      "exclusion_criteria": "...",
      "boundary_notes": "...",
      "completeness": {"definition": true, "criteria": true, "exemplars": true, "near_misses": false, "gold": true},
      "counts": {"positives": 54, "explicit_rejections": 3, "reviewed_negatives": 1300,
                  "read_in_full_items_with_positive": 9, "exemplars": 5, "near_misses": 1},
      "exemplars": [
        {"segment_id": "442850:0051", "dataset_id": "q_61e9e49b4f", "item_id": "442850",
         "group_name": "OpenAI", "person_name": "Sam Altman", "speaker_name": "...",
         "speaker_role": "...", "turn_indices": [0], "text": "...", "note": "...",
         "twin_segment_ids": ["723933:0046"]}
      ],
      "near_misses": [ { "...same fields as exemplars..." } ]
    }
  ]
}
```

Rules:
- `exemplars` and `near_misses` hold **only researcher-flagged passages**. Never fall back
  to model predictions when there are none: export an empty list, and let `completeness` say
  so. Filling the gap with predictions is exactly the seed-pack flaw this plan fixes.
- Twins of an exemplar are listed in `twin_segment_ids`, not repeated as separate exemplars.
- Archived themes are included with their status, since consumers decide whether to use them.
  Merged themes are not included: their codes already point at the target.
- `codebook_version` hashes only the researcher-authored content (`topics`), so external
  runs can record exactly which codebook they used, and two exports of an unchanged codebook
  get the same version.

**`codebook_labels.csv`**: one row per (segment, theme) with a known label. Neutral pairs are
left out (documented in `label_policy`).

| Column | Values |
|---|---|
| `segment_id`, `dataset_id`, `item_id`, `group_name`, `person_name`, `speaker_role` | from `segments` |
| `turn_indices` | K6, `;`-separated |
| `theme_id` | |
| `label` | `pos` / `neg` |
| `label_source` | `code`, `code_rollup`, `explicit_rejection`, `read_in_full_uncoded`, `none_confirmed` |
| `item_read_in_full_status` | K5 status |
| `inferred_by_coverage` | true/false |
| `is_exemplar`, `is_near_miss` | true/false |
| `twin_group` | shared id for twins, blank otherwise |

Build labels from `coding_store/labels.py` (the single source of truth for what counts as
positive or negative). Don't re-derive the rules in the export module.

Document both formats in docs/DEVELOPMENT.md, next to the topic-model export section, and in
the README's export list.

### K8 Point the LLM pipeline at the export

Once K7 ships, make `scripts/llm_topics/stage0_prepare.py` accept `--codebook
<path to codebook_topic_themes.json>` as an alternative to its own hand-edited
`codebook_seed_themes.json`, and take labels from `codebook_labels.csv` instead of inferring
them from `segment_topic_scores.csv`. Field mapping:

| Pipeline (`codebook_seed_themes.json`) | App export |
|---|---|
| `description` | `definition` |
| `include` | `inclusion_criteria` |
| `exclude` | `exclusion_criteria` |
| `boundary_notes` | `boundary_notes` |
| `exemplars[].comment_ids` | `exemplars[].item_id` + `turn_indices` (`comment_id = f"{item_id}_{index+1:04d}"`, after the K6 check) |
| `near_miss_comment_ids` | `near_misses[]`, same conversion |
| gold tier `read_in_full` (≥5 coded segments) | `item_read_in_full_status` in confirmed/interactive, or `inferred_by_coverage`, whichever the researcher chooses |

Keep the old path working: the researcher's hand-edited file from October 2026 is research
material.

## Tests

Rule 8 allows narrow `unittest` coverage for label-building logic. These parts of K7 qualify,
because a mistake silently changes what an external model learns or is scored against:
- the pos/neg/neutral assignment and `label_source`
- read-in-full status (K5)
- the exemplar/near-miss consistency rules (flag only on active rows; cleared when the row
  is soft-deleted)

Put them in `tests/test_codebook_export.py`, using a throwaway `coding.db` in a temp
directory and restoring `schema.DB_PATH` afterwards, as `tests/test_labels.py` does.
Everything else (UI, queue mode, export button) is verified by hand in the browser.

## Verification checklist

- [ ] Migration run on a **copy** of a real `coding.db`; row counts unchanged; the app starts
      against the copy.
- [ ] All interactive checks done with `PROJECT_DIR` set to
      `%LOCALAPPDATA%\InterviewViewer\test-project` (rule 11). Never against the repo root
      or a researcher's project, not even read-only. When handing work to a subagent, say so
      explicitly.
- [ ] Theme editor saves and reloads all four fields; completeness updates live.
- [ ] Exemplar star and near-miss key work; undo clears the flag; removing a code clears its
      exemplar.
- [ ] `boundary` queue lists only judged negatives, and its accept writes a code.
- [ ] "Confirm read in full" sets the new column and leaves `marked_at` unchanged.
- [ ] Export: JSON validates against the documented shape; the same codebook exported twice
      gives the same `codebook_version`; labels CSV matches `labels.py` for a hand-checked
      theme.
- [ ] No JS errors in the browser console on Coding, Review, Model and Web Appendix tabs.
- [ ] `python -m unittest discover -s tests` passes.

## Out of scope

- Calling any LLM or external model from the app. The roadmap defers an in-app LLM few-shot
  classifier; the app's job is to export what such a model needs (D1) and import scores
  back (D2).
- Changing segmentation constants, or re-segmenting.
- Deleting or rewriting the existing `topic_seed_pack` / `segment_topic_scores` exports.

## Questions to ask the researcher before or during the build

1. Completeness thresholds (K2): are 15 words, 3 exemplars from 2 groups and 2 near misses
   right?
2. Should unconfirmed bulk marks stop counting as negatives in training too (K5)? This
   changes model behavior; don't do it by default.
3. Should the export include archived themes by default, or only on request?
4. Is there more than one coder? Codes, rejections and flags are per coder; the export
   currently assumes the default coder.
