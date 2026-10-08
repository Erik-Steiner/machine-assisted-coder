# Plan: codebook rigor, near misses, the codebook export, and in-app LLM topic coding

Agreed 2026-10-07; Part 2 added 2026-10-08. **Part 1** (K1–K8) expands roadmap items **B4**
(memos and codebook rigor) and **D1** (scoring-pack export) into buildable work. **Part 2**
(L1–L9) brings the LLM topic-coding procedure, first run by hand in `scripts/llm_topics/`,
into the app (roadmap D4). Build Part 2 after K3–K7: it consumes everything they add. It is written for a
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

### K3 Marking exemplars and near misses while coding — done 2026-10-08

**As built:** `coding_store/examples.py` (`set_flag()`, `list_flagged()`, `find_twins()`,
`annotate_twins()`); `POST /api/codebook/flag`, `GET /api/codebook/examples`. A ☆ after every
coded chip (Code tab and Review cards) flags an exemplar, with an optional note. In Review queues,
`e` accepts and flags an exemplar and `m` rejects and flags a near miss (also as buttons). These
are recorded as accept/reject plus `review_decisions.flag` (schema v8), so round counts are
unchanged, and `prev_flag`/`prev_flag_note` let undo restore an earlier flag. The Codebook page
lists each theme's exemplars and near misses with notes, a "possible twin" badge, Open in Code,
note editing and Unflag. Twin warnings never block. The rule is word-set Jaccard ≥ 0.5 plus the
same speaker or the same near-duplicate cluster. Undo history in a Review queue now survives
leaving and re-entering the tab. The original spec follows.

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
material. Whichever source it reads, stage 0's checks against current labels (the `usable`
flags and the issue list in `codebook_resolved.json`) must still run. K8 is the bridge for
the scripts; Part 2 replaces them in the app.

## Part 2 — In-app LLM topic coding (L1–L9)

Added 2026-10-08. Part 1 (K1–K8) makes the codebook complete and exportable. Part 2 brings
the procedure that consumed it into the app: discover topics around the researcher's themes,
pilot the coding against the researcher's own codes, then code every executive turn and
produce the deliverables. It reverses the roadmap's standing decision to defer an LLM
classifier: the researcher asked for it on 2026-10-08, after running the procedure by hand.

### Where the procedure stands

It was built and run outside the app as `scripts/llm_topics/` (October 2026). Each stage's
module docstring explains its design. That code is the reference implementation: port its
logic, don't redesign it, and keep its outputs readable by the new code.

| Stage | Script | What it does | Status |
|---|---|---|---|
| 0 Prepare | `stage0_prepare.py` | Turn↔segment map; turn-level gold labels; rank scores; checks the researcher's codebook against current labels | Built and run |
| 1 Discover | `stage1_discover.py` | Sonnet 5.5 proposes emergent topics around the locked themes (map → reduce) | Built and run (~$2) |
| 2 Pilot | `stage2_pilot.py` + `coding.py` | Haiku 4.5 codes 19 gold + 12 random interviews; precision/recall against the researcher's codes | Built, not yet run |
| 3 Full run | not written | Batch API over all ~26,200 executive answer turns; deliverables | Specified in L6 |

Shared modules: `common.py` (paths, models, `STUDY_CONTEXT`), `llm.py` (every API call),
`coding.py` (codebook, prompt, chunking, schema, response checks).

### What the procedure taught (each one cost time or money to find)

1. **Turns, not segments, are the unit an LLM should code.** A segment can be a fragment of
   a turn, or several turns merged, and loses the question it answers. The deliverable is
   keyed by turn (`comment_id`). Labels move from segments to turns by fixed rules: a turn is
   positive if any of its segments is; negative only if every segment is a judged negative;
   otherwise neutral. This needs K6.
2. **The researcher's codebook is the source of truth, and no process may overwrite it.**
   An earlier stage 0 regenerated its own exemplar picks next to the researcher's edited file.
   It now reads the file, never writes it, and writes a separate resolved copy.
3. **Check the codebook against the current labels on every run.** Coding continues while
   the codebook is in use. In October 2026, a near miss (`702100_0006`) became a positive
   when the researcher coded 3 of its 6 segments. An exemplar's interview (`615552`) was
   dropped from scoring as a duplicate record. Such examples are flagged `usable: false` and
   reported, never silently dropped or used.
4. **Prompt examples, and their twins anywhere in the corpus, can't be test items.** Twins
   are found by word-set Jaccard ≥ 0.5 between segments. That is enough even when two
   transcripts of one video are chunked differently: the Daniela/Dario Amodei twin scored
   0.76. Overlap-of-the-shorter (containment) was tried and rejected: it matches short
   interviewer lines to everything.
5. **One turn can be an exemplar for two themes.** Store example↔topic as a list, not a
   single value.
6. **Never show the old classifier's scores to the LLM; use them by rank only.** Inside the
   judged interviews, the stored full-fit `score` has memorized the labels, so ranks there use
   `oof_score`. Ranks serve two purposes: oversampling near the locked themes in discovery,
   and a TF-IDF baseline in the pilot.
7. **Keep the test set out of every step that shapes the codebook.** Discovery never samples
   read-in-full interviews, so the pilot's precision and recall are measured on turns that no
   codebook decision has seen.
8. **Locked themes plus emergent topics.** The seed themes cover only a few percent of
   executive talk. Without emergent topics, most turns would be "other". Discovery may only
   add topics beside the locked themes, never redefine them. It dropped 5 of 139 candidates
   for duplicating them.
9. **API mechanics:**
   - **Always stream.** The SDK refuses a non-streaming request whose `max_tokens` could
     outrun its 10-minute timeout.
   - **Take the request ID from the stream** (`stream.request_id`); a streamed message has no
     `_request_id`.
   - **Write each call's record as soon as it returns.** A crash while building it lost one
     paid call.
   - **Per-model settings:** Sonnet/Opus 5.x get `effort` plus the refusal fallback
     (`fallbacks: "default"`). Haiku 4.5 rejects `effort`, doesn't need the fallback, and
     takes `temperature: 0`.
   - **Prompt caching:** the codebook system prompt (~18k tokens) is cached; Haiku 4.5 only
     caches prefixes of at least 4,096 tokens.
10. **Version everything by content hash.** A codebook edit gives a new `codebook_version`
    and a fresh set of calls. Stored records are reused only for an identical prompt.

### L1 Optional dependency and API key

- `requirements-llm.txt` lists `anthropic`, `python-dotenv` and `pydantic`. As with
  `requirements-embeddings.txt`: imported only when used, never installed by
  `install.bat` / `install.command`, and installed from an in-app button that runs `pip`
  as a `jobs.JobRegistry` job. Copy the `/api/model/install_embeddings_deps` pattern.
- **The key is read from `ANTHROPIC_API_KEY`**, in the project's `.env` or the environment.
  `.env` also holds the ceointerviews.ai `API_KEY` / `BASE_URL` that `query_api.py` uses;
  they are different keys and must not be confused. The UI shows only "key found / not
  found". The key never goes into `coding.db`, a log, a record or an export.

### L2 Storage

LLM output is a **prediction, never a code.** It must not be written to `codes` or
`negatives` (rule 5's provenance argument applies): a researcher code always means the
researcher decided. New tables via `CREATE TABLE IF NOT EXISTS`, so no version bump:

| Table | Holds |
|---|---|
| `llm_runs` | run_id, kind (`discovery` / `pilot` / `full`), scheme_id, model, settings JSON, codebook_version, codebook snapshot JSON, study_context, status, cost, started/finished, coder |
| `llm_calls` | run_id, request key, request JSON (system prompt stored once per run, by hash), response JSON, request_id, served_by_model, usage, created_at. This is `scripts/llm_topics/llm.py`'s record, moved into the database. |
| `llm_turn_codes` | run_id, dataset_id, item_id, turn_index, primary_topic_id, secondary_topic_id, confidence, topic_summary, justification, coded_by (`model` / `rule`) |
| `llm_topics` | run_id, topic_id, name, origin (`seed` / `emergent` / `fixed`), definition, include, exclude, boundary_notes, examples JSON, status (`proposed` / `accepted` / `rejected` / `merged_into`) |

**Open question for the researcher:** where accepted emergent topics live.
- **(a)** As themes in the scheme, with a new `themes.origin` column (schema bump). The
  researcher can then code them, train on them, and flag exemplars like any theme.
- **(b)** Only in `llm_topics`, for this run. Simpler, but they can't be coded by hand.

Recommend (a), as the hybrid deductive–inductive design intends.

**Projection for Review:** for each run and theme, write
`predictions(model_version = "llm_<run_id>", segment_id, theme_id, score)`. A segment scores
the highest confidence among its turns where the theme is primary or secondary (via K6), or
0 otherwise. This reuses D2's import path (the `ext` / `llm` engine prefix), so Review's
queues, top terms and score bars work unchanged. Pin these runs (A7) so pruning never drops
them.

### L3 Prepare (stage 0 in the app)

There is no export step: the app already holds the codebook, labels and segments. Port, as
pure functions in a new `coding_store/llm_prepare.py` (or similar):
- turn-level labels from `labels.py` via K6
- the read-in-full tier, from K5 status (`confirmed` / `interactive`), with the ≥5-coded
  heuristic as a researcher-chosen fallback
- rank scores from the latest TF-IDF run (`oof_score` where present)
- codebook resolution with `usable` flags and a list of issues

**Show the issues before any run.** Show them on the Codebook page, and require
acknowledgement in the run dialog. Examples: "near miss X is now coded with this theme",
"Diffusion has no near misses".

### L4 Discover (stage 1)

A "Discover topics" action on the Codebook page, run as a background job:
- **Sampling:** as `stage1_discover.draw_sample()`:
  - 1,500 executive turns of at least 20 words
  - about 70% random, stratified by group with a floor
  - about 30% from the top decile of a seed theme's rank, spread evenly over themes
  - never from read-in-full interviews or prompt examples
- **Calls:** map batches of 100 turns, then one reduce, on Sonnet 5.5. The prompts are in
  `stage1_discover.py`.
- **Researcher review:** the draft appears as `proposed` topics. Each can be accepted,
  edited, merged into another, or rejected, and accepting creates the theme (L2 open
  question).
- **Example passages:** shown for every proposed topic. A passage becomes a researcher
  exemplar only if the researcher codes and flags it (K3); the model's choice never makes
  it one.
- **What the review must show** (from the October 2026 run):
  - each topic's approximate size; four topics came out at about 1% of the sample
  - the model's notes on which boundaries were hard
  - the candidates dropped for duplicating a locked theme, with reasons

### L5 Pilot (stage 2)

A "Pilot coding" action, as `stage2_pilot.py`:
- **Interviews:** every read-in-full interview, plus 3 random interviews per group.
- **Call settings:** the same prompt and chunking the full run will use (L7), on Haiku 4.5
  at temperature 0.
- **Report** (persisted in `llm_runs` and shown in the UI):
  - per seed theme: precision, recall and F1 with 95% Wilson intervals, both for
    primary-or-secondary and for primary only
  - the TF-IDF baseline on the same turns
  - confidence calibration by band
  - the topic distribution, including the `other` share
- **Disagreements go to Review, not a CSV.** Add a queue mode `llm_disagreement` (same
  shape as K4's `boundary`):
  - LLM yes, gold no: `a` = it is the theme (writes a code; the original coding missed it),
    `r` = reject (confirms the gold)
  - LLM no, gold yes: `a` = keep the code, `r` = the code was wrong
  
  Record every decision in `review_decisions` with `mode='llm_disagreement'`. Report
  precision before and after adjudication: before, it's a lower bound.
- **Gate:** the full run (L6) is offered only after a pilot on the current
  `codebook_version`. If the codebook changes, the pilot must be re-run.

### L6 Full run (stage 3)

Not yet built anywhere; this is its spec.
- **Message Batches API:** one batch request per interview chunk, keyed `custom_id` =
  request key, polled by a `jobs.JobRegistry` job. Results come back in any order: key them
  by `custom_id`, never by position.
- **Batch limits:** the refusal fallback isn't accepted on batches (Haiku doesn't need it).
  Caching inside a batch is best-effort.
- **Validation:** check every result with `coding.check_codes()`. Re-send missing or invalid
  turns as ordinary streamed calls, once. Report anything still missing.
- **Cost:** with the October 2026 codebook (18k-token system prompt), the pilot estimate of
  about $1 per 1,100 turns scales to about $24 for 26,200 turns at standard rates, about
  $12–15 batched. Show the estimate in the run dialog, and spend only after the researcher
  confirms.
- **Logging:** `llm_run` start and finish (model, codebook_version, turns, cost) to
  `activity_log`, with labels in `appendix.js`.
- **Reliability (optional):** re-code a 5% random sample and report run-to-run agreement
  (Cohen's kappa on the primary topic).

### L7 The coding prompt and its rules

Port `coding.py` as is. The rules it encodes are research decisions and must stay visible in
the run's settings and snapshot:
- **Codebook in prompt order:** seed themes (with all their usable exemplars and near
  misses), then emergent topics (with up to 3 examples), then two fixed topics:
  - `other`: substantive, but fits nothing
  - `non_substantive`: no topical content
- **Fields per turn, in this order:** `topic_summary` (≤25 words), `justification` (≤30
  words, naming a ruled-out topic on close calls), `primary_topic_id`, `secondary_topic_id`
  (or `none`), `confidence`. Reasoning comes before the labels.
- **Topic IDs are an enum** in the output schema, so the model can't invent one.
- **Confidence is about the primary topic only**, on a stated scale: about 0.9 for a clear
  case, 0.7 for some ambiguity, 0.5 for borderline, under 0.4 for a guess. It means
  something only once the pilot's calibration table supports it.
- **`PRIORITIZE_SEED_THEMES`:** a clearly present seed theme takes a slot ahead of an
  emergent topic. The researcher chooses this per run.
- **Short answers** take their topic from the question they answer.
- **Rule-coded turns:** executive turns under 5 words become `non_substantive` by rule,
  without a call.
- **Requests:** a stretch of one interview in spoken order. Up to 25 turns to code or 5,000
  words, with the questions between them and 4 turns before as context. Context turns are
  cut to 150 words; turns being coded never are.
- **`STUDY_CONTEXT`:** the paragraph describing the corpus and study goes into every call.
  Make it a per-scheme setting the researcher writes; it steers what counts as a topic.

### L8 Deliverables

An export button on the run, writing into `EXPORTS_DIR`:
- **`<dataset>_interview_turns_coded.csv`:** every row of the turn export
  (`scripts/export_interview_turns.py`'s columns), plus:
  - `coded_scope` (`llm` / `rule_non_substantive` / `context_only` / `excluded_other`)
  - `primary_topic_id`, `secondary_topic_id`, `confidence`, `topic_summary`,
    `justification`, `coded_by`, `codebook_version`, `run_id`
  
  Rows outside the coding scope keep blank codes, and `coded_scope` says why.
- **`topic_summary.csv`:** one row per topic: `topic_id`, `name`, `origin`, `description`,
  `include`, `exclude`, `n_primary`, `n_secondary`, counts per group, mean confidence, two
  example `comment_id`s. `topic_id` joins to both topic columns of the coded file.
- **`methods_note.md`:** model and settings, codebook version, sample sizes, pilot metrics
  before and after adjudication, cost. Written for a manuscript's supplementary material,
  next to the Web Appendix.

### L9 Verification

- **Pure functions, unit-tested** (rule 8's label-building exception: they decide what the
  model is graded against):
  - turn↔segment map
  - turn label roll-up
  - codebook resolution and `usable` flags
  - twin detection
  - `check_codes()`
  - Wilson intervals and precision/recall
- **No API calls in tests.** Use stored records, as `parse_call` replays them; a run whose
  calls are all recorded costs nothing.
- **Reference parity:** on the October 2026 data, the in-app prepare step must reproduce
  `turns_prepared.parquet`'s labels and flags and `codebook_resolved.json`'s issues exactly.
- **Test project only** (rule 11). API calls cost money even against the test project. Use
  a tiny dataset there, and confirm spend in the dialog every time.

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

- In Part 1, calling any LLM or external model from the app: Part 1 only makes the codebook
  complete and exportable. Part 2 adds the LLM calls, as separate items, built after K3–K7.
- Writing LLM output into `codes` or `negatives`, ever (L2). Only the researcher writes codes;
  an accepted disagreement in Review writes a code because the researcher decided it.
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
5. Part 2: should accepted emergent topics become real themes in the scheme (L2 option a,
   recommended) or stay inside one LLM run (option b)?
6. Part 2: should `PRIORITIZE_SEED_THEMES` default to on? It favours the seed themes for the
   two topic slots, which raises their recall and makes the emergent topics' counts less
   comparable.
7. Part 2: the run dialog's spending cap. Should runs over a threshold (say $25) need a
   second confirmation?
