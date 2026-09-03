# Bringing your own data in

This app was originally built around one source (the ceointerviews.ai API), but the coding,
Model, and Review tabs only need a corpus of speaker-turn segments — they don't care where the
text came from. This guide covers the canonical schema every dataset shares, and the four ways to
get your own data into it.

## The canonical record schema

Every dataset in `queries/` (or `demo_dataset.json` / `executive_interviews.json`) is a JSON file
holding a flat array of objects shaped like this:

```json
{
  "group_name": "the organization/community/subreddit this item is about",
  "person_name": "the item's primary subject -- the interviewee, the post author",
  "person_title": "that person's job title/role, or null",
  "person_id": "a stable id for that person, or null",
  "item_id": "a unique string id for this item -- MUST be unique within the dataset",
  "item_title": "a short title for this item",
  "source_url": "a link back to the original, or null",
  "publish_date": "ISO 8601 date/time, or null",
  "duration_secs": null,
  "view_count": null,
  "like_count": null,
  "combined_classifier_score": null,
  "source_name": "the specific channel/publication/platform, or null",
  "transcript_text": "the full text, speaker-labeled, e.g. \"[Name] said this\\n[Other] replied\"",
  "turns": [
    {
      "speaker_name": "who is speaking in this turn",
      "speaker_id": "a stable id for that speaker, or null",
      "speaker_role": "interviewer | subject | other | null -- see below",
      "depth": 0,
      "timestamp": "elapsed time, an ISO date, or null",
      "content": "the exact text of this turn"
    }
  ]
}
```

A few fields are worth calling out because they're easy to conflate:

- **`person_name` vs. `speaker_name`**: `person_name` is the *item's* primary subject (the
  interviewee, a Reddit submission's author). `speaker_name` is *per-turn* attribution — could be
  the interviewer, could be a different commenter replying in a thread. They're often the same
  value for a single-subject interview, but they're two different concepts.
- **`speaker_role`**: nullable, used for exactly one thing — excluding turns from coding and
  topic modeling by default. Set it to `"interviewer"` for a moderator/interviewer's questions
  (they'll still display in the Coding tab for context, but their theme-chip controls are
  disabled, and `classifier.py` never trains on them). Leave it `null` for ordinary content — a
  Reddit comment, a forum post, an interview subject's answer. `"other"`/`"subject"` are also
  valid values with no special exclusion behavior; they're just labels.
- **`depth`**: nullable integer, `0` by default. Only meaningful for threaded sources (Reddit):
  `0` = a top-level item, `1` = a direct reply, `2`+ = a reply to a reply. The Coding tab indents
  segments by this value so a thread reads visually nested.
- **`item_id` must be unique within the dataset**, and stable across re-imports — the Coding
  subsystem derives each segment's permanent id from it
  (`f"{item_id}:{seg_index:04d}"`), so a changing `item_id` orphans any codes already applied.

`duration_secs`/`view_count`/`like_count`/`combined_classifier_score` are optional engagement
metrics — leave them `null` if they don't apply to your source. `transcript_text` is used by
Browse's transcript renderer and full-text search; if you don't build it yourself, the importers
below derive it from `turns` automatically.

## Four ways to get data in

### 1. Search & Export (the live API)

If you have ceointerviews.ai credentials, the Search & Export tab in the app does everything for
you interactively — no files to prepare.

### 2. `download_script.py` (bulk, same live API)

```
python download_script.py "Company Name" "Another Company"
```

Also requires ceointerviews.ai credentials. See `README.md` for setup.

### 3. Interview transcripts — in-app upload, or `scripts/import_interview_transcript.py`

**No command line needed:** the Search & Export tab's **Import your own data** section has an
**Interview transcript** panel. Pick a `.docx`/`.txt`/`.json` file; for a `.docx`/`.txt`
transcript you'll be shown the speaker labels found and asked to mark each one
interviewer/respondent/other (the browser equivalent of the CLI prompts below) plus the same
metadata fields, then click Import. A `.json` file is validated and imported directly, no extra
input needed. Either way the dataset shows up in Browse immediately, same as a Search & Export
download -- and unlike a live download, it's segmented into `coding.db` as part of the same
Import click, so it's ready for the Coding tab right away (click "Start coding this dataset" on
the result, or open the Coding tab and pick it from the dataset dropdown). The server-side logic
is identical either way (`scripts/import_interview_transcript.py`'s
`parse_turns`/`bridge_backchannels`/`assign_roles`/`build_record`, called from
`viewer_server.py`'s `/api/import/transcript/parse` and `/commit`) -- the CLI below and the
in-app panel produce the same output.

**Grouping interviews into one bucket.** By default every import creates its own new dataset --
fine for a one-off transcript, but a research project with many interviews (analogous to how a
company download's many executives all live under one dataset) usually wants them all in one
place. The transcript panel's **Add to** dropdown offers every dataset you've previously created
this way (`kind="interview_import"` in `queries/_index.json` -- live/downloaded company datasets
aren't offered, since appending a hand-uploaded interview into a live query result would conflate
the two); pick one instead of "a new dataset" and the new interview is appended into that
dataset's existing `queries/*.json` file rather than creating a new one, and its registry `count`
and label update to match (`query_api.append_to_dataset()`). The CLI equivalent is
`--append-to <dataset_id>` (find the id in `queries/_index.json`). Either way, if the new
interview's `item_id` happens to already exist in that dataset (same group/person/item
title/publish date hashing to the same id -- see `deterministic_item_id()`), the import is
rejected rather than silently overwriting that item's segments; adjust the item title or publish
date, or pick a different dataset.

The CLI script remains available for scripted/batch use. Two input modes, chosen by file
extension:

**A Word "Transcribe"-style `.docx` or `.txt` file** — repeated `HH:MM:SS[ Speaker Label]` lines,
each followed by that turn's text (this is what Microsoft Word's built-in Transcribe feature
produces, and what many other transcription tools' plain-text export looks like too). No LLM
needed — this format is parsed deterministically. You'll be asked which speaker label is the
interviewer and which is the respondent:

```
python scripts/import_interview_transcript.py transcript.docx
```

Answer the interactive prompts, or skip them for scripted/batch use:

```
python scripts/import_interview_transcript.py transcript.docx \
    --interviewer "Speaker 1" --respondent "Speaker 2" \
    --person-name "Jane Doe" --group-name "Acme Corp" \
    --item-title "Q3 strategy interview" --publish-date 2024-03-01
```

A speaker label can also be marked "other" (or you can leave a Transcribe artifact's unlabeled
turns as "other") — those turns are shown with a distinct badge and included in neither the
interviewer exclusion nor guaranteed inclusion; decide those case-by-case in the Coding tab.

Before roles are even assigned, the parsed turns are automatically cleaned up: Transcribe-style
tools give every pause-detected fragment its own timestamped turn, so one person's answer often
arrives as a dozen 3-10 word turns, sometimes with a brief backchannel from the other speaker
("Right.", "Hmm.") interjected mid-sentence. `bridge_backchannels()` merges consecutive
same-speaker turns into one continuous passage and bridges *across* a short, non-question
interjection so it doesn't split an answer in two — the interjection itself is kept, just
repositioned to sit right after the merged passage instead of in the middle of it. A short turn
that ends in "?" is never bridged, since it's a real follow-up question, not an acknowledgement.
Nothing is ever deleted or reworded — every original turn still exists in the output, just
merged into a same-speaker neighbor's text and/or reordered relative to a bridged interjection.

**A raw transcript in any other format** — paste it into an LLM with the prompt template below,
save the LLM's JSON output, then run the same script on that file:

```
python scripts/import_interview_transcript.py llm_output.json
```

<details>
<summary>LLM prompt template (click to expand)</summary>

```
You are converting a raw interview transcript into a structured JSON file for a
qualitative-coding research tool. Output ONLY a JSON array (no prose, no markdown
fences) containing exactly one object with this shape:

[
  {
    "group_name": "<the organization/company/community this interview is about, or null>",
    "person_name": "<the interview subject's full name>",
    "person_title": "<the subject's job title/role, e.g. 'CEO of Acme Corp', or null>",
    "item_title": "<a short title for this interview, e.g. 'Q3 2024 strategy interview'>",
    "publish_date": "<the interview date in YYYY-MM-DD format, or null if unknown>",
    "source_url": null,
    "turns": [
      {
        "speaker_name": "<who is speaking in this turn>",
        "speaker_id": null,
        "timestamp": "<elapsed time in the recording as mm:ss, or null>",
        "content": "<the exact words spoken in this turn, no paraphrasing>",
        "speaker_role": "interviewer" | "subject"
      }
    ]
  }
]

Rules:
1. Split the transcript into turns at every speaker change.
2. Tag every turn's "speaker_role" as exactly "interviewer" (asking questions /
   moderating) or "subject" (being interviewed / the research subject). If truly
   ambiguous, use "subject" -- err toward including content in the codable set.
3. Preserve the speaker's actual words. Do not summarize, correct grammar, or omit
   filler words unless inaudible ("[inaudible]" is fine verbatim).
4. If there are no explicit timestamps, set "timestamp" to null rather than guessing.
5. Output valid JSON only -- no commentary, no markdown code fences.

Here is the raw transcript:

<paste your raw transcript here>
```

</details>

### 4. Reddit / Arctic Shift — in-app upload, or `scripts/import_reddit.py`

**No command line needed:** the Search & Export tab's **Import your own data** section has a
Reddit / Arctic Shift panel below the transcript one. Pick the submissions `.jsonl` file (and
optionally the comments `.jsonl` file), click Parse to see a preview -- item/turn counts,
subreddits found, and any orphaned-comment count -- then click Import. No role assignment needed
here (see below). Like the transcript panel, it's segmented into `coding.db` as part of the same
Import click -- ready for the Coding tab right away. The server-side logic is identical to the
CLI (`scripts/import_reddit.py`'s `build_records`/`build_comment_tree`/`walk_comments`, called
from `viewer_server.py`'s `/api/import/reddit/parse` and `/commit`).

The CLI script remains available for scripted/batch use. Takes Arctic Shift's export shape:
submissions and comments as separate JSONL files (one JSON object per line).

```
python scripts/import_reddit.py --submissions posts.jsonl --comments comments.jsonl
```

`--comments` is optional — a submissions-only import is valid, you just get single-turn items.

One canonical item per submission, with the submission body as turn 0 and its comments as
subsequent turns, ordered by a **real depth-first walk of the reply tree** (reconstructed from
each comment's `parent_id`) — not flat chronological order. This matters: in a representative
sample, well over half of all comments reply to *another comment*, not the submission itself, so
a flat ordering would misrepresent most of the conversation. Siblings (replies to the same
parent) are ordered chronologically. The Coding tab renders this indented by `depth` so the
thread reads visually nested.

Field mapping, for reference:

| Arctic Shift field | Canonical field |
|---|---|
| `id` | `item_id` |
| `subreddit` | `group_name` |
| `author` | `person_name` / per-turn `speaker_name` |
| `author_fullname` | `person_id` / per-turn `speaker_id` |
| `title` | `item_title` |
| `created_utc` | `publish_date` / per-turn `timestamp` (ISO 8601) |
| `permalink` | `source_url` (`https://reddit.com` + permalink) |
| `score` | `like_count` — the closest honest analog; Reddit has no view-count metric, and `combined_classifier_score` is a ceointerviews.ai-specific field we don't repurpose |
| `selftext` | turn 0's `content` (empty for link-only posts — that submission still becomes an item, just with only its comments as turns) |

`speaker_role` is left `null` for every Reddit turn — there's no interviewer/subject structure,
so everything is ordinary codable/trainable content by default.

**Known simplifications** (documented, not built this pass): deleted/removed comments
(`body` = `"[deleted]"`/`"[removed]"`) are kept as placeholder turns rather than dropped, so any
real replies underneath them keep correct depth — the min-word-count filter (see below)
naturally deprioritizes these low-content placeholders without special-case logic. A per-comment
`score` isn't captured as its own field (no natural home in the turn shape yet).

### Not built yet

A **generic CSV/tabular importer** with a column-mapping config (for "any other scraped forum or
online data" not already covered above) is planned but not implemented — no representative
example was available when this was designed. In the meantime, hand-convert a small dataset to
the canonical JSON shape above.

## After importing: making a dataset codeable

The in-app **Import your own data** panels (interview transcript, Reddit/Arctic Shift) segment
the dataset into `coding.db` automatically as part of Import -- it's ready for the Coding tab
immediately, no extra step. Every other path (Search & Export's live download, `download_script.py`,
or running an import script directly from the command line) only writes to `queries/`; before a
dataset from one of those shows up in the Coding tab, run:

```
python scripts/build_segments.py
```

With no arguments this picks up every dataset currently in `queries/_index.json` (idempotent —
safe to re-run any time, and harmless to run even on a dataset the in-app panel already
segmented). See `README.md` for the full setup walkthrough.

## Finding near-duplicate interviews

The live API (and, in principle, any source) can hand you the same real-world speech more than
once: multiple outlets each independently re-transcribe the same event, so the same content lands
under several different `item_id`s. This isn't a hypothetical — a single Feb 2026 India AI Summit
keynote by one Anthropic executive showed up 12 times across 12 different YouTube channels in this
project's own corpus, several of which had already been coded before the duplication was noticed.
Left alone, this skews `classifier.py`'s training corpus: a near-identical passage repeated across
several "different" items inflates its term weighting and its apparent label support, and can leak
across cross-validation folds (a duplicate's twin sitting in the "held-out" fold isn't really held
out).

```
python scripts/find_duplicates.py                       # scan every dataset in queries/_index.json
python scripts/find_duplicates.py q_87b8c86188            # scan just this dataset (still checked
                                                            # against every other included dataset)
python scripts/find_duplicates.py --verbose                # also show rejected near-miss pairs
python scripts/find_duplicates.py --promote-canonical q_87b8c86188:713324
```

This is a **mark-only** audit: it never deletes, hides, or archives a `queries/*.json` record, and
never touches `codes` — nothing about Browse, Coding, or Review changes by running it. It groups
same-person items published within a few days of each other, compares their full text (TF-IDF
cosine similarity — handles a short clip fully contained in a much longer transcript correctly,
which a simple diff doesn't), and records detected clusters in `coding.db`, each with a suggested
"most complete" canonical copy.

**Once you've run it, `scripts/train_classifiers.py` respects the result automatically** — every
non-canonical duplicate's segments are excluded from the training corpus by default, even ones that
already have codes (those codes stay completely intact in `coding.db`, just not consulted for
training). The script's printed report always calls out any cluster where an already-coded copy
isn't the suggested canonical one — resolve those by hand: either recode the canonical copy, or run
`--promote-canonical` to make the already-coded copy canonical instead (no codes are migrated
either way — two independently-transcribed duplicates don't share segment boundaries reliably
enough to migrate codes between them automatically). Pass `--include-duplicates` to
`train_classifiers.py` if you ever want one training run without the exclusion.

**Once a run exists, Browse and Coding flag it too**, so you don't have to remember which items
are duplicates by heart: a non-canonical item gets a small "⧉ duplicate" tag in the list and a
fuller "near-duplicate — excluded from classifier training" notice with a **View the canonical
copy** link in the detail header (jumps straight to it, switching datasets first if the canonical
copy happens to live in a different one); the canonical item itself gets a quiet "canonical of N
duplicates" note. Nothing here is enforced — it's purely informational, so you can still code (or
remove codes from) any copy you choose.

**The detector isn't perfect — correct it by hand, right from the flag.** No algorithm change
needed: every flag doubles as a control.
- A wrongly-flagged item shows **Not a duplicate** next to the notice — click it to keep that item
  in training regardless of what the run said. An unflagged item shows **Mark as duplicate…** --
  click it, then either pick from the same-person candidates shown (same dataset) or type
  `dataset_id:item_id` directly (needed for a cross-dataset duplicate, or any pair the detector
  never compared) to exclude it from training as a duplicate of that item.
- Either correction is durable: it's recorded in `coding.db`'s `duplicate_overrides` table
  (`coding_store.add_duplicate_override()`), independent of any one detection run, so it survives
  a future `find_duplicates.py` re-run instead of being silently recomputed away. It always wins
  over whatever the automated run says, and never touches `codes` or `queries/*.json`.
- Change your mind any time via the **Undo** button that appears in place of the correction
  control once applied.

## The "min word count" filter

Real-world text data — especially short-form Reddit comments — often has a meaningful share of
fragments too short to carry topic signal (a one-word reply, a "This."). The Coding and Review
tabs both have a live "Min words" filter: segments below the threshold are hidden from view
(never deleted), and `classifier.py`'s training corpus applies the same floor by default
(`MIN_SEGMENT_WORDS`, currently 5) so short fragments don't get trained on either.

## Troubleshooting

**"my dataset didn't validate"** — the error names the exact file and problem (see
`query_api.DatasetValidationError`). Most often: the top level isn't a JSON array, an entry isn't
an object, or (when segmenting) a record has no `item_id`.

**segments don't show up in the Coding tab** — the dataset hasn't been segmented yet. Run
`python scripts/build_segments.py`, same as for any other source.

**`item_id` collision / segments silently overwriting each other** — every record's `item_id`
must be unique within its dataset and stable across re-imports. The interview-transcript importer
generates one deterministically from your metadata if you don't supply one, specifically so
re-running an import on unchanged input doesn't shuffle ids.
