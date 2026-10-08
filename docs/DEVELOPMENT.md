# Development and Production Guide: Machine Assisted Coder

## Purpose

This dashboard shows one record at a time from a large JSON data file. A researcher moves through records in order, filters by metadata, and copies text with a citation tag. This guide explains how the dashboard works. Use it to build the same dashboard for a different dataset.

The app has grown five more tabs since the design below was written -- Coding, Model, Review,
Analytics, and Web Appendix, covered in their own sections further down. The core pattern this
file describes (light index + full detail, no build step, no framework) still applies to
Browse/Search & Export; Coding/Model/Review/Analytics/Web Appendix are a separate subsystem
layered on top, backed by SQLite instead of flat JSON. `viewer_server.py`'s module docstring is
the authoritative, complete endpoint list for all seven tabs -- the numbered list just below is
Browse/Search & Export only, kept here because this section doubles as the adaptation guide
those two tabs are built around.

The data model itself is source-agnostic (interview transcripts, Reddit threads, or any other
speaker-turn text) -- see [IMPORTING_DATA.md](IMPORTING_DATA.md) for the canonical record schema
and every documented way to get data in.

## Architecture

The dashboard has two parts: a server and a frontend, and two tabs: Browse (reading a dataset) and Search & Export (fetching a new one from the live API).

The server is `viewer_server.py`. It reads the primary data file once at startup, as `dataset_id="primary"`. It splits each record into two views: a light "index" view (small metadata fields) and a full "detail" view (all fields, including large text fields). Beyond the primary dataset, the server can hold any number of other datasets — each one a query result saved by the Search & Export tab into `queries/`, loaded into memory lazily the first time it's requested and cached afterward. `query_api.py` is the API client used by Search & Export: it wraps `get_companies`/`get_entities` keyword search and paginated `get_feed` fetching, and knows how to flatten a feed item into the row shape used for export.

Endpoints:

1. `GET /api/datasets` returns every dataset currently known (primary + saved queries) with id, label, and count.
2. `GET /api/index?dataset=<id>` returns the index view for every record in one dataset, sorted by publish date.
3. `GET /api/interview/<id>?dataset=<id>` returns the full detail view for one record in one dataset. Record ids are only unique *within* a dataset, so the dataset must be specified.
4. `GET /api/search/companies?keyword=..` and `GET /api/search/entities?keyword=..` proxy the live API's keyword search, for the Search & Export tab's disambiguation lists.
5. `POST /api/query/start` kicks off a background thread that pages through `get_feed` for a chosen `company_id` or `entity_id`, builds export rows, and writes them to `queries/<kind>_<slug>_<timestamp>.{json,csv}`; it also appends an entry to `queries/_index.json` so the new dataset survives a server restart. Returns a `job_id`.
6. `GET /api/query/status?job_id=..` is polled by the frontend to report progress (`items_fetched`, `pages_fetched`) and, on completion, the new dataset's id.
7. `GET /api/transcript_search?dataset=<id>&q=<term>` returns the ids of records in that dataset whose `transcript_text` contains `<term>` (case-insensitive substring match), by scanning the dataset's full in-memory records server-side. This is what backs the "Interview contains" filter — `transcript_text` is deliberately left out of the index payload (see Data contract below), so a client-side substring filter isn't possible without it.

The frontend is three static files in `viewer_static/`: `viewer.html`, `app.js`, and `styles.css`. The browser loads `viewer.html`, which loads `app.js`. On load, the script fetches `/api/datasets`, populates the dataset dropdown, and loads the selected one via `/api/index?dataset=<id>` into memory. All filtering and sorting happen in the browser, using this small index. The script fetches the full record from `/api/interview/<id>?dataset=<id>` only when the researcher views that record, and caches it (the cache is cleared on every dataset switch, since ids aren't globally unique). Search & Export is a separate, independent chunk of `app.js`: it drives the keyword search, the disambiguation click-to-select flow, and polling a query job to completion, then hands off to the same `loadDataset()` function Browse uses once a new dataset is ready.

## Why this design

A raw data file can hold gigabytes of text across thousands of records. Loading all of that text into the browser at once is slow. This design splits each record into a small part (metadata) and a large part (content). The browser only ever holds one dataset's worth of small metadata, plus the few full records the researcher has already viewed.

The server's Browse endpoints use only the Python standard library — reading a dataset and serving it needs no install step. Search & Export is the exception: it talks to the live ceointerviews.ai API, so it needs `requests` and `python-dotenv` (already in `requirements.txt`) and a valid `.env`. If those aren't installed or configured, Browse still works fine on whatever's already in `queries/` or the primary dataset — only the Search & Export tab's requests will fail, with the error surfaced in the UI rather than crashing the server.

The frontend uses plain HTML, CSS, and JavaScript. It has no build step and no framework. A developer or an AI agent can open `app.js` and read the whole file in one pass.

## Data contract

The server expects a JSON file that holds a single array of flat objects. Each object is one record. Each record needs:

- A set of small fields for metadata (strings, numbers, short IDs). Use these fields for filtering, sorting, and the list view.
- One or more large fields for content (the text the researcher reads). Do not put content fields in the index view.
- A field to sort by. This guide uses `publish_date`, but any orderable field works, such as a document number or a timestamp.

If a record does not have a field, use `None` (Python) or `null` (JSON) as its value. The frontend code already treats missing values as empty.

## Steps: adapt the dashboard for a new dataset

1. Save the new dataset as a single JSON file, structured as a flat array of objects.
2. List which fields are metadata and which fields are content. Content fields are the ones you do not want in the index payload.
3. Open `viewer_server.py`. In `build_dataset()`, change the field name passed to `.pop()` for content fields, so the index view drops them.
4. In `_sort_key()`, change `publish_date` to your dataset's order field.
5. Open `viewer_static/app.js`. Update the `uniqueSorted()` calls in `buildFilterOptions()` to match your metadata field names.
6. Update `matchesFilters()` so each filter checks the correct field name.
7. Update `renderMeta()` and `buildCitationTag()` so the header and citation tag show your dataset's fields.
8. If your content field is not plain, line-broken text, update `renderTranscript()`.
9. Start the server and open the page in a browser. Confirm the index loads, filters narrow the list, and a detail record loads when selected.

## File reference

`viewer_server.py`
This file loads the primary dataset, manages the dataset registry (primary + saved queries), runs Search & Export's background jobs, and registers every endpoint as a `routing.py` route. Change this file when you add, rename, or remove a metadata or content field, when you change the sort order, when you touch the dataset/job machinery itself, or when you add a new endpoint (register it with `@ROUTER.get(...)`/`@ROUTER.post(...)` near the other routes for its tab).

`routing.py`
The route table `viewer_server.py`'s endpoints register into, plus the shared request-handling adapter every route goes through: JSON body parsing (`json_body=True`, or `"optional"` for a route that should tolerate a missing/invalid body), declarative required-field checks (`required=[...]`), and path-param 404 lookups (`loaders={...}`). A route handler is a plain `(Request) -> Response` function with no dependency on `BaseHTTPRequestHandler` -- `Handler` in `viewer_server.py` is a thin adapter that turns a socket request into a `Router.dispatch()` call and writes back whatever `Response` it returns. Change this file when the *shape* of request handling needs to change (a new kind of body, a new response type); change `viewer_server.py` for a new endpoint using the existing shapes.

`query_api.py`
This file is the API client for Search & Export: `search_companies`/`search_entities` (keyword search), `fetch_feed` (paginated `get_feed`), and `build_row`/`build_transcript_text` (turning one feed item into an export row). Change this file when the export row shape needs to change, or when the API's parameters change.

`jobs.py`
A second small seam alongside `routing.py`: `JobRegistry`, the thread-safe `job_id -> status
dict` registry behind every "kick off a background thread, poll it to completion" flow --
`viewer_server.py`'s `JOBS` (Search & Export downloads), `TRAIN_JOBS` (Model training),
`SEGMENT_JOBS` (segmenting a dataset into `coding.db` -- see `run_segment_job()`, the Coding
tab's Datasets panel), and `DUPLICATE_JOBS` (a near-duplicate scan -- see `run_duplicate_job()`,
also the Datasets panel) are all instances of it. `start(initial, target, *args)` spawns the
worker thread and seeds the
status dict; `get(job_id)` backs a `GET .../status?job_id=` poll endpoint; `mutate(job_id, fn)`
lets the worker thread read-then-write a field (an increment, a list append) under the lock,
with `update(job_id, **fields)` as sugar for outright replacing fields; `has_running()` backs
`switch_project()`'s guard. A worker function's own exception is caught by `start()` and turned
into `status="error"` automatically -- it doesn't need its own try/except for that. No
dependency on anything `viewer_server.py`-specific, same as `routing.py` -- change this file
when the job-tracking shape itself needs to change; change `viewer_server.py` to add a new job
*type* using the existing shape. The matching frontend half is `app.js`'s `pollBackgroundJob()`.

`viewer_static/viewer.html`
This file defines the page layout for both tabs: header (with tab buttons and the dataset dropdown), Browse's filter panel/jump list/metadata box/transcript box, and Search & Export's search box, result columns, and export panel. Change this file when you add a new filter control, a new metadata field, or a new Search & Export control.

`viewer_static/app.js`
This file holds all frontend logic. Browse logic (fetching a dataset, filtering, navigation, rendering, copy-with-citation) is unchanged in spirit from the single-dataset version, just parameterized by `state.datasetId`. Search & Export logic (keyword search, disambiguation, starting/polling a query job) is a separate, mostly independent chunk at the bottom of the file. Most dataset-specific changes happen in the Browse logic. `pollBackgroundJob(statusUrl, {onRunning, onDone, onError})`, defined near the top alongside `escapeHtml()`, is the shared poll loop behind Search & Export's own job polling and `model.js`'s training-job polling (see `jobs.py`'s `JobRegistry` for its backend half) -- a third polled job type should reuse it rather than writing another recursive `setTimeout` loop.

`loadDataset()` also fetches `/api/duplicates/status` alongside the index and caches it as
`state.duplicates` (keyed by `item_id`, not the dataset-local numeric `id`) -- empty until
`scripts/find_duplicates.py` has been run, or a manual override exists. `duplicateListFlagHtml()`
renders a small "⧉ duplicate" flag in list rows (`renderList()`); `duplicateSectionHtml()` renders
the fuller notice plus correction controls in the Browse/Coding detail headers
(`renderMeta()`/`coding.js`'s `renderCodingMeta()` -- both call the same shared helper, since
`#interviewList` and this data are shared between the two tabs). The detector isn't perfect, so
every non-compact rendering is also a correction UI, not just a display: an unflagged item offers
"Mark as duplicate…" (`duplicateCandidateChipsHtml()` suggests same-person records from the
current dataset; a free-text `dataset_id:item_id` input covers cross-dataset or detector-missed
pairs), a wrongly-flagged item offers "Not a duplicate", and anything already manually corrected
offers "Undo". All of it is one delegated `click` listener in `bindEvents()` (`.dup-jump-link`/
`.dup-mark-open-btn`/`.dup-mark-submit-btn`/`.dup-not-duplicate-btn`/`.dup-undo-*-btn`, each
walking up via `.closest(".dup-section")` -- deliberately no per-item element `id`s, since Browse
and Coding can both have the same item's markup live in the DOM at once) calling
`submitMarkDuplicate()`/`applyDuplicateOverride()`/`removeDuplicateOverride()`
(`POST /api/duplicates/override`(`/remove`)) or `jumpToDuplicateCanonical()` (switches datasets
first if the canonical copy lives in a different one -- cross-dataset duplicates are real, see
`scripts/find_duplicates.py`'s docstring -- and always calls the extracted `resetFilters()` first
so an active filter can't hide the target). Every write refetches `/api/duplicates/status` via
`refreshDuplicateStatus()` rather than patching `state.duplicates` locally, since a single
override can change more than one item's displayed "canonical of N" count.

`viewer_static/styles.css`
This file holds the visual style. It needs no change for a new dataset, unless the layout does not fit the new content.

`coding_store/`
SQLite storage for the Coding subsystem, split by concern rather than one flat module --
each caller imports the submodule it needs (`from coding_store import themes`):
`schema.py` (the SQLite schema, connection, and migration -- every other submodule
depends on this one, it depends on none of them), `segments.py`, `themes.py`, `codes.py`,
`model_runs.py` (Model tab storage), `review.py` (the Review tab's recode queue and
coding-progress counts -- depends on `codes.py`/`model_runs.py`), `activity_log.py`
(Web Appendix: the research-action log, training-run hyperparameters, and the codebook
export), `duplicates.py`, and `dataset_status.py`. `themes.py`/`duplicates.py`/
`dataset_status.py` depend on `activity_log.py` (every mutation there is logged); nothing
depends the other way. Change the submodule that owns the concern you're touching; change
`schema.py` only for the schema/migration machinery itself (later phases' embeddings/
predictions/clusters tables already live in `schema.SCHEMA`, unused until a submodule
reads/writes them).

`segmentation.py`
Turns one item's `turns` into coding segments. Change this file only deliberately -- see
its docstring on why changing the chunking constants after coding has started orphans
existing codes.

`scripts/build_segments.py`
One-off/idempotent precompute script: reads `queries/*.json` and populates
`coding.db`'s `segments` table. With no argv it auto-discovers every dataset currently
registered in `queries/_index.json` (via `query_api.load_registry`) and segments all of
them -- no hardcoded dataset ids, so this works unmodified against whatever a machine's
`queries/` holds. Pass specific dataset ids as argv, e.g.
`python scripts/build_segments.py q_abc123 q_def456`, to segment only a subset. Scripted/batch
use only now -- a researcher using the app itself should use the Coding tab's Datasets panel's
"Segment" controls instead (`viewer_server.py`'s `run_segment_job()`), which can also reach
`primary`/`demo`, the two datasets this script structurally cannot (they're never registered in
`queries/_index.json`).

`viewer_static/coding.js`
Frontend logic for the Coding tab: codebook management, segment rendering, code
toggling, keyboard shortcuts. Loaded after `app.js` and shares its `state` object.

`classifier.py`
TF-IDF + logistic-regression classifier: one shared vectorizer fit on the whole
corpus, one calibrated model per theme. `run_training_pass()` is the shared
orchestration used by both `scripts/train_classifiers.py` and the Model tab's
background training job -- change this file when the model or feature
representation changes. `build_corpus()` excludes non-canonical near-duplicate
segments by default once `scripts/find_duplicates.py` has been run -- see that
script and `coding_store/schema.py`'s `duplicate_*` tables.

`scripts/train_classifiers.py`
Thin CLI wrapper around `classifier.run_training_pass()`. Safe to re-run any
time; each run gets its own `model_version` so history accumulates.

`viewer_static/model.js`
Frontend logic for the Model tab: train/retrain job polling, theme list with
quality badges, and the selected-theme detail panel (metrics, history,
top predictive terms, exemplar segments). Loaded after `coding.js`; shares
`app.js`'s `state` and `coding.js`'s `codingState` (for the "View in Coding"
hook) rather than duplicating navigation logic.

`viewer_static/review.js`
Frontend logic for the Review tab: theme multi-select, the recode queue
(currently-coded + model-flagged-uncoded candidates), and code-toggling that
tags additions `source="recoded"`. Loaded after `model.js`; reuses
`coding.js`'s `buildChipsHtml`/`postCode`/`deleteCode`/`jumpToSegmentInCoding`
rather than reimplementing segment-card rendering.

`appendix_export.py`
Renders the Web Appendix tab's activity log (`coding_store.activity_log.get_appendix_feed()`) into a
self-contained HTML file with inline CSS -- no new dependency, opens standalone, and gives
a PDF for free via the browser's own "Print to PDF".

`viewer_static/appendix.js`
Frontend logic for the Web Appendix tab: fetches and renders the activity log, and the
"Download appendix"/"Download codebook" buttons. Loaded after `review.js`.

`install.bat`, `start.bat`, `install.command`, `start.command`
Setup/launch scripts, the primary way a non-developer researcher gets this app running -- see
the README's "Setting this up" section. The `.bat` pair is Windows, the `.command` pair is
macOS (double-clickable from Finder); each pair mirrors the other's steps and reasoning exactly,
so a change to one side's logic should be mirrored on the other. `install.bat` detects Python
(`py -3`, falling back to `python`); `install.command` detects it via `python3` (falling back to
`python` if it reports itself as Python 3). Either installer creates a virtual environment and
installs `requirements.txt` into it; the matching `start.*` script runs the server from that
environment and lets `viewer_server.py`'s own `webbrowser.open()` call (in `main()`) open the
browser once the port is actually bound, rather than polling. Both pairs deliberately put the
virtual environment outside this project folder -- `%LOCALAPPDATA%\InterviewViewer\venv` on
Windows, `~/Library/Application Support/InterviewViewer/venv` on macOS -- because a venv sitting
inside a cloud-synced folder (Dropbox, OneDrive, ...) fights that sync client's own file locks
during install, confirmed on a Windows machine as a real, repeatable failure, not a hypothetical
one. `install.command`/`start.command` are committed with the executable bit set and are pinned
to LF line endings via `.gitattributes` (`*.command text eol=lf`) -- a CRLF shebang line breaks
bash's interpreter parsing on macOS, which a Windows clone's `core.autocrlf` would otherwise
silently introduce. `viewer_server.py`'s `main()` also retries the next few ports on `OSError`
if the requested one is taken -- note that `ThreadingHTTPServer`/`HTTPServer` default to
`allow_reuse_address = True`, which on Windows lets a second process silently bind onto a port
another process is still listening on instead of raising `OSError`; `main()` sets it back to
`False` so a genuinely occupied port actually triggers the retry instead of two processes
quietly fighting over the same port.

`paths.py`
The shared `queries/`/`coding.db`/`exports/` locations for whichever project is active *at
process startup* (`PROJECT_DIR`, defaults to the repo root). Imported by every script/module
that touches those locations. `data/demo_dataset.json`/`executive_interviews.json` deliberately
stay anchored to `viewer_server.py`'s own location instead (bundled convenience files, not a
project's own research data).

`project_registry.py`
The known-projects list backing the in-app **Project** picker (`projects_registry.json` at
the repo root, gitignored). Deliberately holds no notion of "the active project" itself --
this app supports more than one `viewer_server.py` process running at once against different
projects, so "active" only makes sense per-process (see `viewer_server.py`'s
`CURRENT_PROJECT_DIR`/`switch_project()`); a shared "active" field in this file would let one
process's switch corrupt what a *different* running process's UI reports. `viewer_server.py`
live-switches a running process's `QUERIES_DIR`/`coding_store.schema.DB_PATH` by reassigning
those module globals directly (`coding_store.schema.set_db_path()`) -- no restart, works
because `coding_store.schema.get_conn()` looks up `DB_PATH` at call time, not a captured value.

`scripts/import_interview_transcript.py`, `scripts/import_reddit.py`
Import a researcher's own data (interview transcripts, Reddit/Arctic Shift exports) into
`queries/`, in the same canonical shape every other dataset uses. See
[IMPORTING_DATA.md](IMPORTING_DATA.md) for the full format and usage. Both scripts' parsing
(`parse_transcript_turns`/`bridge_backchannels`/`assign_roles`/`build_record`/
`normalize_json_records` for transcripts; `build_records`/`summarize_records` for Reddit) is also
called directly by `viewer_server.py`'s `/api/import/transcript/*` and `/api/import/reddit/*`
endpoints, which back the Search & Export tab's **Import your own data** section
(`viewer_static/import.js`) -- a no-command-line alternative to running either script by hand.

**Two `.docx`/`.txt` transcript formats, one dispatcher.** `parse_transcript_turns(lines)` tries
`parse_turns()` (the original timestamped-line parser) first, then falls back to
`parse_labeled_turns()` -- returning `(turns, format_name)` so both the CLI and
`/api/import/transcript/parse` can tell the researcher which format matched
(`format_name`/`"format_detected"`; `import.js` surfaces it in the upload status text). Both
parsers produce the identical turn shape (`{timestamp, speaker_label, content}`), so everything
downstream -- `bridge_backchannels`, role assignment, `build_record` -- is completely format-
agnostic and needed zero changes when the second format was added.

`parse_labeled_turns()` handles the "Speaker: text" shape (no timestamp) that covers hand-typed
transcripts, diarization-tool exports, and ChatGPT/Claude's own default transcript formatting
alike (see IMPORTING_DATA.md for the researcher-facing writeup and format examples). It never
tries to identify which tool produced the file -- it detects a *pattern*: `LABELED_TURN_RE`
matches a short (`<= LABEL_MAX_WORDS`), optionally `**bold**`-wrapped label immediately before a
colon at the start of a line, and a candidate label only becomes a confirmed speaker (turn
boundary) once it's recurred `>= LABEL_MIN_OCCURRENCES` times with `>= 2` distinct labels overall
-- the same signal a human uses to tell a real speaker cue from an incidental colon in the body
text ("Note: recorded over Zoom."), which only ever appears once and so gets folded into the
surrounding turn instead of starting a new one. A markdown-bold label wrapped in literal `**`
needs this text-level handling, but a label pasted into Word *with* formatting kept doesn't --
`python-docx`'s `paragraph.text` already strips run-level bold, so that case is already plain
`"Speaker: text"` and needs no special-casing at all (confirmed by inspecting
`data examples/Interview Self-transcribed.docx`'s paragraph/run structure while building this).

The browser upload path gets the file's raw text into `viewer_server.py` two different ways,
and the difference matters. The transcript panel base64-encodes the file client-side
(`import.js`'s `arrayBufferToBase64()`) and sends it as one `content_base64` field in a JSON
`POST /api/import/transcript/parse` body -- fine for a single interview file, but a
multi-hundred-MB Arctic Shift comments export blows past a browser tab's memory budget that way
(the original bytes, a base64 string ~1.33x their size, and another same-size-ish copy when
`JSON.stringify` serializes the request body, several alive at once -- reproduced as an
out-of-memory tab crash on a real ~350 MB comments file). So the Reddit panel instead posts each
file directly as a request body (`import.js`'s `uploadFileRaw()`, `POST
/api/import/raw_upload?filename=..`) -- the browser streams a `File`/`Blob` body to the network
without ever materializing it as a JS string -- and `viewer_server.py` stores the decoded text in
`RAW_UPLOADS`, keyed by an `upload_id` the follow-up `POST /api/import/reddit/parse` call
references (small JSON body, just the id) instead of carrying the content itself. If the
transcript panel ever needs to handle files at this scale, it should switch to the same
`raw_upload` path rather than growing a second base64 special-case.

Every import defaults to creating its own new dataset, but a researcher can instead add the new
interview(s)/submission(s) into an existing one (`query_api.append_to_dataset()`) -- both import
panels' **Add to** `<select>` (`viewer_static/import.js`'s shared `populateImportTargetOptions
(selectId, kind)`, fed by `GET /api/datasets`, filtered client-side to `kind === "interview_import"`
for the transcript panel or `"reddit_import"` for the Reddit panel) or, for transcripts only so
far, the CLI's `--append-to <dataset_id>` (`scripts/import_reddit.py` has no CLI equivalent yet).
`append_to_dataset()` reads the target's existing `queries/*.json`, rejects
(`DatasetValidationError`) if any new record's `item_id` already exists in it (same collision
`validate_dataset_records()` guards against on create -- silently allowing it would let the new
record's segments overwrite the existing item's in coding.db), otherwise rewrites the same
`json_file`/`csv_file` with the combined records and updates the registry's `count`/`label`
(`relabel_with_count()` regenerates the "N `<count_noun>`" suffix -- `"interview(s)"` by default,
`"submission(s)"` for a Reddit dataset, so appending never mislabels a dataset with the wrong
noun). `viewer_server.py`'s two commit handlers call `_append_dataset()`, which re-segments only
the newly-added record(s) (not the whole dataset -- the existing ones are already in coding.db)
and evicts the target dataset_id from the in-memory `DATASETS` cache so the next `get_dataset()`
reloads the appended file from disk instead of serving the stale cached copy -- the same
cache-invalidation idiom `switch_project()` uses.

`_create_dataset()`/`_append_dataset()` (both in `viewer_server.py`) are the shared "write it to
queries/, register it, maybe segment it, log it" ceremony behind every dataset-creating/appending
endpoint -- a Search & Export download (`run_query_job`), a transcript-import commit, and a
Reddit-import commit each just build `records`/`label` and call one of the two, rather than
repeating the write/register/segment/log steps by hand. Change these, not each call site, when
that ceremony itself needs to change.

`bridge_backchannels()` (`import_interview_transcript.py`) runs right after
`parse_transcript_turns()`, on every transcript import path (both formats -- see above). Word's
Transcribe (and similar tools) gives every pause-detected fragment its own timestamped turn, so a
short backchannel from the other speaker ("Right.", "Hmm.") interjected mid-sentence otherwise
splits the person actually talking into two disconnected turns -- bad raw material for both
coding and the classifier's embeddings; a hand-typed or LLM-formatted transcript has the same
issue on a smaller scale (a transcriber's own "mm-hmm" written mid-answer). It merges in three
phases: (1) unconditionally coalesce every run of literally-consecutive same-speaker turns (no
length cap -- `segmentation.py`'s later sentence-aware splitting handles cutting the result back
down to size); (2) bridge a short, non-question block sandwiched between two blocks from the same
(different) speaker -- the flanking blocks merge into one continuous passage, and the bridged
block is kept as its own turn, just repositioned to sit right after instead of splitting the
passage in two; (3) a final pass merges any turns still left immediately adjacent with the same
speaker. Phase 3 exists because phase 2's flanking-merge only looks at what's already in its
`result` list, not at what comes later in the original sequence -- a bridged interjection
repositioned right before a *later*, non-bridgeable turn from that same interjecting speaker (a
short "Right, right." pulled out of the middle of an answer, immediately followed by that
speaker's next real question) landed as two consecutive same-speaker turns instead of one. Found
by testing against `data examples/Interview Self-transcribed.docx` (the speaker-labeled format's
own sample) -- present in 8 of that file's 40 pre-phase-3 turns, and also latent in the original
Word Transcribe sample (`interview example/Interview Transcript.docx`'s post-bridge count
tightened from 192 to 172 turns once phase 3 was added, word count still exactly preserved). A
block ending in "?" is never treated as bridgeable ("What about session 0?" is a real
topic-changing follow-up, not an acknowledgement) -- this was a real false-positive found and
fixed during earlier testing on `interview example/`'s sample transcript, along with an earlier
bug where a short turn's "who do I return to" bookkeeping got stuck pointing at the *interjecting*
speaker instead of the original one. Nothing is ever dropped: every turn from
`parse_transcript_turns()` still exists somewhere in the output, just reordered and/or merged
into a same-speaker neighbor's `content`.

`scripts/find_duplicates.py`
Retroactive audit for near-duplicate items in `queries/*.json` -- the same real-world
speech independently re-transcribed by several outlets, each landing under its own
`item_id` (confirmed, not hypothetical: one executive's speech at a single event showed
up 12 times in this project's corpus). The actual scan is `run_duplicate_scan()` (`dataset_ids`,
`window_days`, `threshold`, `progress_callback`) -- this module's own `main()` is a thin
argparse + print wrapper around it, same shape as `classifier.run_training_pass()` /
`scripts/train_classifiers.py`. `viewer_server.py`'s `DUPLICATE_JOBS`/`run_duplicate_job()`
(the Coding tab's Datasets panel "Scan for duplicates" button) call the same function, so the
CLI and the in-app control always run identical detection logic. `--promote-canonical` stays
CLI-only -- the in-app correction UI (mark/unmark as duplicate) already covers that need day to
day. Blocks candidates by `(person_name, publish_date within a few days)`, scores pairs by
whole-document TF-IDF cosine similarity (a fresh item-level vectorizer, not `classifier.py`'s
segment-level one), and clusters above-threshold pairs via union-find. Mark-only: writes
clusters + a suggested canonical
member to `coding_store/schema.py`'s `duplicate_runs`/`duplicate_clusters`/
`duplicate_cluster_items` tables (see `coding_store/duplicates.py`); never deletes/hides
a `queries/*.json` record or touches `codes`. The detector isn't perfect -- a separate
`duplicate_overrides` table (`duplicates.add_duplicate_override()`/
`remove_duplicate_override()`) holds researcher
corrections (both directions: "this isn't a duplicate" and "this is, of that one"),
deliberately not tied to any one `duplicate_runs` row so a correction survives the next
re-run instead of being silently recomputed away; `get_duplicate_status_for_dataset()`
merges the two for display, always letting overrides win. See
[IMPORTING_DATA.md](IMPORTING_DATA.md)'s "Finding near-duplicate interviews" section for
usage (including the in-app correction UI and the CLI's `--promote-canonical`), and
`classifier.py`'s `build_corpus()` for how training consults both tables.

`scripts/migrate_field_names.py`
One-time migration for a `queries/`/`coding.db` from before the schema_version 2 field
rename (company/executive/feed_item_id/enhanced_transcript_json -> group_name/person_name/
item_id/turns). Not needed on a fresh clone.

## Coding subsystem

The Coding tab lets a researcher build a codebook of themes and apply them to
speaker-turn segments of each interview. It is a separate subsystem from Browse/Search
& Export: those stay flat-JSON and in-memory; Coding is backed by a SQLite database,
`coding.db` (gitignored, like `queries/` -- it's local, regenerable-except-for-the-
researcher's-actual-codes data, not source). See the `coding_store` package for the
schema and the reasoning for using SQLite here.

Setup: after `queries/` has at least one dataset (from `download_script.py` or Search &
Export), run `python scripts/build_segments.py` once to populate `coding.db` with segments
(idempotent -- safe to re-run after a dataset grows). Then `viewer_server.py` serves the
Coding tab's endpoints against that database; no separate process is needed.

`segmentation.py` turns one item's `turns` into segments (~120-word chunks, merging short
same-speaker fragments and splitting long turns at sentence boundaries -- see the module
docstring for the exact constants and why they must not change once real coding has
started). `viewer_static/coding.js` is a second frontend file, loaded after `app.js`, so
neither file grows past a single readable pass; it reuses `app.js`'s shared `state`
(dataset/interview navigation) and calls back into it only through two small hooks
(`window.onEnterCodingTab`, `window.onDatasetLoaded`) rather than duplicating navigation
logic.

Two source-agnostic segment attributes, both threaded through from `turns` by
`segmentation.py` and stored on `segments`: `speaker_role` (nullable -- `"interviewer"`
segments display for context but get no theme chips, and are excluded from
`classifier.py`'s training corpus; every other value, including `null`, is ordinary
codable content) and `depth` (nullable integer, reply-nesting depth for threaded sources
like Reddit -- `coding.js` indents segment cards by this value). Neither is populated for
data segmented before schema_version 2; both are `null` there, which is the same as "not
interviewer" / "not nested", so old data behaves exactly as before.

Coding is keyboard-driven in the frontend: `j`/`k` or arrow-down/up move focus between
the current interview's segment cards without the mouse, and number keys `1`-`9`
toggle the corresponding codebook theme (in list order) on the focused segment --
`toggleFocusedSegmentTheme()` in `coding.js`. `postCode`/`deleteCode`/`buildChipsHtml`
live in `coding.js` rather than `app.js` specifically so `model.js` and `review.js` can
reuse them without a dependency on Browse-only code.

Themes support **edit** (name/description), **archive** (soft-hide from new coding,
existing codes untouched -- `set_theme_status`), and **merge** (`merge_into`: every
code under the source theme is re-pointed to the target theme in place, keeping its
`code_id`/coder/source/note/created_at so provenance survives; a segment that already
has an active target code from the same coder gets its now-redundant source code
soft-deleted instead of duplicated; the source theme is then archived with
`merged_into` set, not deleted, so a merge is auditable and reversible by hand -- see
`coding_store/themes.py`'s `merge_themes()`). There is no hard delete for a theme, by
design: codes are load-bearing research data.

**Explicit rejections (`negatives`, live since schema v5).** A rejection records that a
coder looked at a segment and decided a theme does *not* apply, which is different from never
having coded it. It has the same shape as `codes` (`coder`/`source`/`note`/`deleted_at`) and is
never hard-deleted. A code and a rejection for the same (segment, theme, coder) cancel each
other: `codes.add_code()` soft-deletes the rejection and `codes.add_negative()` soft-deletes the
code, in the same transaction. `merge_themes()` moves rejections like codes, and where a coder
ends up with both on one segment, the code wins. The table-level `UNIQUE(segment_id, theme_id,
coder)` also covers soft-deleted rows (unlike codes' partial index), so re-rejecting reactivates
the existing row. Today rejections don't change training labels (an uncoded segment is already
a 0). They keep a rejected prediction out of future Review queues, are excluded from the Model
tab's "flags N more" and coverage counts, appear struck through on chips, and are exported as
`negatives.json/csv` and in `segment_topic_scores`' `rejected` column. Roadmap A6's `reviewed`
negative mode will train on them. Tests: `tests/test_labels.py`.

**Effective labels and sub-code roll-up (`coding_store/labels.py`).** What counts as theme T is
computed on read, never stored. A code on T *or any sub-code of T* (any depth, archived
sub-codes included) makes a segment positive for T. A rejection of T *or any ancestor of T*
makes it a negative, so rejections flow down, never up. A positive wins over an inherited
rejection. These rules are used everywhere it matters: `classifier.build_labels()` (training
positives; runs record `roll_up: true` in their params), `model_runs`' `already_coded`/
`already_rejected` flags and `get_value_prop()` counts, and `review.get_progress()`'s per-theme
counts (now distinct segments, so "x/25 coded" matches the `n_pos` training will see). Review's
"Currently coded" list is deliberately still exact: it's a recode view of the codes you
actually applied. In the Code/Review chips, a parent whose sub-code is coded shows a dashed
"implied" outline. Tests: `tests/test_labels.py`.

**Negative modes and "reviewed" state (roadmap A6).** Training takes a `negative_mode`
(`classifier.NEGATIVE_MODES`). In `reviewed` (the default), a theme is fitted only on its effective
positives plus `labels.reviewed_negative_segment_ids()`: explicit rejections, and uncoded segments
the researcher judged for that scheme *after the theme was created*. A segment counts as judged
if its item was marked "fully coded" (`item_reviews`) or it was confirmed "none of these" with the
Code tab's `n` key (`segment_none_confirmations`); see `coding_store/reviewed.py`. Unread
segments are left out of fitting and CV metrics but still scored, and their `oof_score` is NULL.
A theme with fewer than `MIN_NEGATIVES_REVIEWED` (50) judged negatives is skipped with status
`skipped_insufficient_negatives`, and the Train tab says so. `all_uncoded` is the original
behavior (every uncoded corpus segment is a negative). The mode is recorded in each pass's
`model_run_params` and shown per run in the Train tab's history; runs from before A6 read as
`all_uncoded`. Marks and confirmations are never deleted (`unmarked_at`/`unconfirmed_at`) and
are included in the codebook export. For projects coded before marks existed, the Code tab's
Datasets panel can bulk-mark interviews that have codes in the scheme
(`reviewed.backfill_candidates()`/`bulk_mark_items()`, `POST /api/coding/item_reviews/bulk`).
Each mark is dated either to that interview's last active code in the scheme (the default, so
themes created after you last worked on it aren't covered) or to a chosen moment.
`item_reviews.recorded_at` (schema v6) keeps the real time of entry, so a back-dated mark never
claims to have existed earlier than it did. Tests: `tests/test_labels.py`
(`ReviewedNegativeTests`, `BulkMarkTests`, `ReviewedTrainingPassTests`).

`coding_store/schema.py`'s `SCHEMA` also defines `cluster_runs`, `clusters`,
`cluster_segments`, and `consistency_checks` tables. These exist for later phases
(unsupervised topic clustering, inter-rater consistency checks) and are created by
`init_db()`, but nothing in the current server or frontend reads or writes them yet. Don't
treat their presence in the schema as a feature that exists -- check for an `/api/...`
endpoint or a call site before relying on one. The `embeddings` table is the other
exception: it's now wired up, by `coding_store/embeddings.py` and
`classifier.build_embeddings()` -- see "Full-corpus export and the embedding engine"
below.

## Model subsystem

**Sub-tabs (CODEBOOK_PLAN U1).** Train (Codebook | Models, opening on Models) and Review
(Queues | Recode, opening on Recode) share one helper in `app.js`: `setupSubnav(tab, navEl,
{initial, onChange, beforeChange})` shows the `.subpage[data-subpage]` elements of the nav's page
that match the clicked `.subnav-btn[data-subpage]`, and hides the rest. A page can mark several
elements with one name (Review marks sidebar controls and main panes). `showSubpage(tab, name)`,
`currentSubpage(tab)` and `goToSubpage(tab, name)` (switch top-level tab and sub-tab in one step,
for deep links) work from any later script. `beforeChange` returning false keeps the current
sub-tab (the Codebook page's unsaved-edits prompt).

**Train tab sub-pages.** `codebook.js` registers the Codebook | Models sub-tabs (`#modelSubnav`) at the
top of `#modelView`. The Models page is the original sidebar plus detail (`model.js`,
`storage.js`). The Codebook page (docs/CODEBOOK_PLAN.md K2) edits each theme's definition
(`themes.description`), `inclusion_criteria`, `exclusion_criteria` and `boundary_notes` (schema
v7) through the existing `POST /api/codebook/themes/<id>/update`, and shows
`GET /api/codebook/overview`: counts plus `coding_store/codebook.py`'s `theme_completeness()`
checks (definition 15+ words, both criteria, 3+ exemplars from 2+ groups, 2+ near misses, a
positive in a read-in-full interview). They are advisory and never block anything: the Models
page shows a one-line note, and the Code tab's theme rows show a clickable "Codebook n/5" badge.
Schema v7 also adds `codes.is_exemplar`/`exemplar_note`, `negatives.is_near_miss`/
`near_miss_note` and `item_reviews.read_in_full_confirmed_at`. Flags count only on active rows,
and a code or rejection re-applied after removal starts unflagged (`decisions.undo()` restores
rows, flag included, on purpose). Tests: `tests/test_codebook.py`.

**Segment → turn linkage (CODEBOOK_PLAN K6).** Segments merge short same-speaker turns and split
long ones, and the `segments` table doesn't record which turns each came from.
`segmentation.segment_turn_indices(record)` does: `result[k]` is the list of 0-based indices into
`record["turns"]` behind segment `seg_index` k. It shares `_segment_pieces()` with
`segment_interview()`, so the segmentation itself is unchanged (rule 4 still applies to its
constants). Turn exports (`scripts/export_interview_turns.py`) number turns 1-based over the
whole list, so `turn_id = index + 1`. Tests: `tests/test_segmentation.py`.

**Read in full (CODEBOOK_PLAN K5).** `reviewed.mark_status()` classifies an active mark as
`confirmed` (`read_in_full_confirmed_at`), `interactive` (`recorded_at == marked_at`),
`bulk_unconfirmed`, or `unknown` (pre-v6, no `recorded_at`). Only the first two count as read in
full (`READ_IN_FULL_STATUSES`; used by `read_in_full_items()`, the codebook's "gold" check, and
`negative_mode="read_in_full"`). `inferred_by_coverage` (5+ coded segments in the scheme) is
reported alongside but never changes the status. Confirming never touches `marked_at`. The segment
payload's `item_review` carries `status`/`coded_segments`/`inferred_by_coverage`.
`coding.js`'s `openInterviewInCoding()` opens an interview only if it's in Browse's current
(filtered) list and reports otherwise; `openNextUnconfirmed()` walks the unconfirmed list in order.

**Exemplars and near misses (CODEBOOK_PLAN K3).** `coding_store/examples.py` sets and lists the
flags (`codes.is_exemplar`/`exemplar_note`, `negatives.is_near_miss`/`near_miss_note`). A flag
needs an active row of the right kind (400 otherwise), and flagging warns about twins: an
already-flagged passage of the same kind for the theme with word-set Jaccard ≥ 0.5 and the same
`person_name` or near-duplicate cluster. The warning never blocks. The UI: `coding.js`'s
`exemplarStarHtml()`/`toggleExemplar()` (a star after every coded chip, shared by the Code tab and
Review), Review's `e`/`m` triage keys, which record accept/reject plus `review_decisions.flag`
(schema v8, with `prev_flag`/`prev_flag_note` for undo), and the Codebook page's lists
(`GET /api/codebook/examples`, with `twin_of`). Segment, queue and recode payloads carry
`exemplar_theme_ids` for the stars. A Review queue's undo stack is keyed by
`model_version:theme:mode`, so it survives the reload that happens when the tab is re-entered.

The Model tab trains and monitors the per-theme classifiers built on top of
the codebook -- read-only monitoring and interpretability, not an editing surface
(no accept/reject-into-codebook actions; that would be a future active-learning
step). It's additive to the Coding subsystem's `coding.db`: three more tables
(`model_runs`, `predictions`, `model_top_terms`) hold everything the tab shows, and
nothing it does touches `segments`/`themes`/`codes`.

Training retrains every theme with enough coded segments in one pass, sharing one TF-IDF
vectorizer fit across all of them (fit once for vocabulary consistency and efficiency, not
per theme) -- there's no per-theme train button. A pass is scoped to one coding scheme
(`run_training_pass(scheme_id=...)`; the Model tab sends the active scheme, and a "Train
every scheme's themes instead" checkbox omits it). The vectorizer is still fit on the whole
corpus either way, so scoping changes which themes get a new run, not any theme's scores.
`scheme_id` is recorded in `model_run_params` and the `training_run` log entry (`null` =
every scheme). The embedding pass is scoped the same way, including its readiness check, so
one scheme reaching the 150-positive floor can't unlock a pass over another. `POST /api/model/train`
starts a background thread (`TRAIN_JOBS`, a `jobs.JobRegistry` instance in `viewer_server.py` --
see "Background jobs" below -- kept separate from `JOBS`, Search & Export's fetch-job registry, so
the two can't collide) that calls `classifier.run_training_pass()`; `GET
/api/model/train_status?job_id=` polls it, same shape as Search & Export's job
polling. A theme's top predictive terms are computed once, at training time (not
on-demand), because the fitted model object itself is never persisted -- only its
predictions and metrics are, so recomputing terms later against however the corpus
looks *then* would silently drift from the coefficients that actually produced that
run's stored predictions.

`viewer_static/model.js` follows the same pattern `coding.js` established: a second
frontend file loaded after it, sharing `state` and `codingState` rather than
duplicating them, calling back into `coding.js` through `codingState`'s
`pendingFocusSegmentId` field ("View in Coding" on an exemplar segment switches
Browse's dataset, positions on the right interview, and tells the Coding tab which
segment to scroll to and focus once it loads).

### Full-corpus export and the embedding engine

Two additions sit on top of the Model tab above, both purely additive -- neither
changes `get_top_predictions`'s/`get_review_candidates`'s capped behavior, which stays
the researcher's day-to-day iterative-coding surface.

**Two scores per prediction (schema v4).** Each `predictions` row has `score`, from the
final model fit on every label, and `oof_score`, the out-of-fold score from the
cross-validation fold model that never saw that segment (`classifier.train_theme()` returns
it alongside the metrics it already produced). A coded segment's `score` is inflated by
construction, because the final model trained on it as a positive. Use `oof_score` whenever
the question is "does the model agree with this label?" (e.g. finding likely mislabels).
`oof_score` is NULL for pre-v4 runs and for any engine/import without a CV pass; treat NULL
as "no honest score available", never as 0. The stored `oof_score`s reproduce the run's
logged PR-AUC exactly. `segment_topic_scores` exports both columns.

**Codebook export for models (CODEBOOK_PLAN K7).** `codebook_export.py` writes
`codebook_topic_themes.json` (per topic: definition, criteria, boundary notes, completeness,
counts, and the researcher-flagged exemplars and near misses with `turn_indices` and twins
collapsed) and `codebook_labels.csv` (one row per known segment × theme label). It builds labels
from `coding_store/labels.py` and only attributes each label's source; it never re-derives the
rules. Positives roll up from sub-codes. Negatives are
`reviewed_negative_segment_ids(read_in_full_only=True)` minus uncoded interviewer turns.
`codebook_version` hashes everything in `topics` except `counts`/`completeness`. Turn indices
come from `segmentation.segment_turn_indices()` on records loaded via `load_records` (the server
passes `_load_dataset_records_with_turns`). Unlike `topic_seed_pack`, whose `exemplars` are the
model's own top predictions, this export only ever contains passages the researcher chose.

**Full-corpus application.** `predictions` already holds one score per (segment, theme,
model_version) for the *entire* corpus a theme trained against -- `run_training_pass()`
never capped it, only the UI's reads did (`get_top_predictions(..., limit=...)`).
`model_runs.get_predictions_for_theme()` (no `LIMIT`, optional score threshold) and
`model_runs.get_corpus_coverage()` (coded + flagged-uncoded as a fraction of the whole
corpus) expose that directly. `topic_model_export.py` writes this out as
`segment_topic_scores.json`/`.csv` (every segment x theme, full precision) and
`topic_seed_pack.json`/`.csv` (per-theme top terms + top exemplars) via `GET
/api/model/export_full_corpus` -- for a separate, unrelated pipeline (comparing this
codebook's classifier against another topic-modeling method, or seeding a
guided/LLM-based one). This app never does that comparison itself.

**Embedding engine.** `classifier.py`'s training functions (`train_theme`,
`build_labels`, `_best_threshold`) only need a feature matrix and a label vector --
they don't care whether the matrix is TF-IDF or dense sentence embeddings. So
"bootstrap a richer classifier" reuses that exact pipeline unchanged, just swapping
`fit_vectorizer()` for `classifier.build_embeddings()` (cached sentence embeddings, via
`coding_store/embeddings.py` and `paths.EMBEDDINGS_DIR` -- one `.npy` matrix file per
model, since SQLite is a poor fit for large dense vectors; the `embeddings` table only
records each segment's row index into that file). Its `model_version`s are prefixed
`embed_v1_` (vs. the TF-IDF pass's `tfidf_v1_`), sharing the same `model_runs`/
`predictions` tables -- the Model tab's TF-IDF/Embedding toggle just picks a different
`model_version` for the same `/api/model/interpret` endpoint. No `model_top_terms` rows
are saved for `embed_v1_*` versions (embedding dimensions aren't interpretable
vocabulary terms the way TF-IDF coefficients are).

**Engine-aware "latest run".** Because both engines share `model_runs`, "a theme's latest run"
is ambiguous. `model_runs.get_latest_model_runs(engine=...)` takes `"tfidf"`/`"embed"` and
returns only that engine's latest run; without `engine` it returns whichever run is newest,
which is fine for engine-independent facts like `n_pos` (`embedding_readiness()` uses it that
way) but wrong anywhere *scores* are shown. An embedding run would otherwise silently take
over those views for the themes it trained. So the Coding/Model sidebar badges always request
`engine=tfidf`, and the Review tab has a "Model scores from" selector (default TF-IDF) that it
passes to `/api/coding/review_candidates`. Each predicted reason carries `engine` and
`model_version`, and the pill shows the engine. The prefix ↔ engine mapping lives in
`model_runs.ENGINE_PREFIXES` (mirrored by `model.js`'s `engineOfVersion()`); add a new engine
there, not as another hardcoded prefix check.

Gated behind `classifier.embedding_readiness()`: `installed` (is the *optional*
`sentence-transformers` package importable -- see `requirements-embeddings.txt`, never
part of the default `requirements.txt` install, since it pulls in PyTorch) and
`volume_ready` (has at least one theme's latest run cleared `TRUST_POSITIVES`, the same
150-positive rule-of-thumb floor already used for "trust this ranking" -- stricter than
`MIN_POSITIVES_ATTEMPT`, since this is explicitly gated on having enough human-verified
volume to bootstrap from, not just enough to attempt at all). `classifier.py` never
imports `sentence_transformers` at module load time; `build_embeddings()` does it
lazily and raises a clear, actionable error if it's missing, so the app runs
identically whether or not the optional package is installed.

A researcher doesn't need a terminal to cross that `installed` gate: `POST
/api/model/install_embeddings_deps` runs `pip install -r requirements-embeddings.txt`
(via `sys.executable`, so it lands in whichever Python is already running this server)
as a background job -- `INSTALL_EMBEDDINGS_JOBS`, the same `jobs.JobRegistry` pattern as
every other long-running action in this app -- behind an "Install embedding
dependencies" button on the Model tab. Idempotent (pip no-ops if it's already
installed), so the endpoint doesn't need to check `embedding_readiness()` first, only
guard against two installs racing (`has_running()`).

### Storage: pruning old runs' scores and compacting

`predictions` grows by one row per in-scope segment for every theme in every training run,
and nothing removes old runs automatically. Measured on a real project: about 280 bytes per
row with indexes, or roughly 18 MB per trained theme per run. The Model tab's **Storage…**
button (`viewer_static/storage.js`) opens a panel backed by `coding_store/storage.py`:

- **The rule** is `storage.plan_prune(runs, pinned_versions, keep_latest)`, a pure function
  covered by `tests/test_storage.py`. Runs are grouped by (theme, engine). Each group's newest
  run is always kept ("latest"), the next `keep_latest - 1` are kept ("recent"), and any run in
  `model_run_pins` is kept ("pinned"). Everything else is "prunable". `keep_latest` must be
  at least 1.
- **Pruning** (`POST /api/storage/prune`, a `PRUNE_JOBS` background job) deletes only
  `predictions` rows for the (model_version, theme_id) pairs the researcher confirmed. It
  re-checks every pair against a fresh `plan_prune()` both in the endpoint and inside
  `prune_predictions()`, and one protected pair refuses the whole request. It never touches
  `codes`, `negatives`, `themes`, `model_runs`, `model_top_terms`, or `model_run_params`, so a
  pruned run keeps its metrics and history row. Logged as `predictions_prune`. Measured on a
  copy of a real 1.24 GB database: 3.4M rows took about 160 s.
- **Compacting** (`POST /api/storage/compact`, `COMPACT_JOBS`) runs `VACUUM` and then
  `wal_checkpoint(TRUNCATE)`, because SQLite only reuses freed pages and never shrinks the file
  on its own. It refuses unless free disk space is at least twice the live database size (WAL
  mode writes the compacted copy through the WAL). Logged as `database_compact`. On that same
  copy, after pruning, it took 7 s and shrank the file from 1,242 MB to 431 MB.
- **Concurrency:** `viewer_server.maintenance_conflict()` is the single overlap rule. Prune and
  compact never run together or alongside training, training won't start during either, and
  `switch_project()` refuses while either runs.
- **Sizes are estimates:** Python's bundled SQLite has no `dbstat`, so per-run sizes come from
  average field widths (`_estimate_bytes_per_row()`). The space actually freed is measured
  from `freelist_count` after the delete.

## Analytics subsystem

A separate tab, Analytics, gives the researcher a corpus-level picture of scale and
density -- document/segment/word counts, vocabulary size, lexical diversity -- before
any coding or modeling is examined, plus a metadata breakdown (bar/line/scatter,
auto-detected from the field's shape) and a vocabulary-overlap panel for categorical
breakdowns. It's read-only, like Model, and touches no schema of its own beyond one
new helper query (`coding_store.segments.get_segments_by_ids()`).

The corpus this tab measures is deliberately the *exact same* one `classifier.py`
trains on -- `corpus_analytics.get_corpus_scope()` calls `classifier.build_corpus()`
directly rather than re-deriving its filters (interviewer turns, sub-5-word
fragments, near-duplicates, excluded/unloaded datasets), so a researcher can never see
one segment count on this tab and a different one implied by Model tab's metrics.
`corpus_analytics.py` is otherwise pure -- it only imports `coding_store.segments`/
`classifier`, never `viewer_server` -- because the one join it can't do itself (a segment's
`dataset_id`/`item_id` to that document's full metadata record: `publish_date`,
`duration_secs`, `view_count`, ...) needs `viewer_server.DATASETS`/`get_dataset()`,
which live in the web layer. `viewer_server._build_item_meta()` does that join once per
request and passes the result into `corpus_analytics.py`'s functions as a plain
`{(dataset_id, item_id): record}` dict.

Breakdown field type (categorical/temporal/quantitative) is detected from the data
every time, never hardcoded to a fixed field list -- a dataset with metadata fields
this app has never seen before still gets offered as a breakdown option. Vocabulary
size/lexical diversity use a plain lowercase+word-token pass (no stopword removal);
vocabulary overlap deliberately uses the *same* preprocessing as `classifier.py`'s
TfidfVectorizer (lowercase + `ENGLISH_STOP_WORDS` removed), since mismatched
preprocessing there would inflate the shared-vocabulary percentage with function
words. Charts are hand-rolled SVG built the same way the rest of this frontend builds
HTML (template literals into `innerHTML`) -- no charting library, so the tab keeps
working with no internet access, same as every other tab.

## Review subsystem

The Review tab is where recoding/refinement happens -- the one place in the
app besides Coding itself where codes actually change. It exists because Coding's
per-interview navigation makes a deliberate re-triage pass across many interviews
impractical: qualitative coding routinely needs revisiting as theme boundaries
sharpen (two themes turning out to overlap, say), and there was no way to see
"every segment coded under theme X" without clicking through interviews one at a
time.

`coding_store/review.py`'s `get_review_candidates(theme_ids, predicted_limit=30)` assembles the
queue: every segment currently coded under any of the selected themes, plus (for
themes with a trained model) each theme's top-scored segments that aren't coded
under that specific theme yet -- so refining a theme's definition also surfaces
good candidates the researcher hasn't seen, not just a re-sort of old decisions.
Each candidate carries a `reasons` list explaining why it's in the queue, which
`review.js` uses to split the UI into two sections ("Currently coded" vs.
"Model-flagged, not yet coded") so the researcher's own past judgment is never
visually confused with the model's guess.

Recode actions are auditable without any new schema: `coding_store.codes.add_code()`
already accepted `source`/`note` parameters (built during Coding, unused until
now); `review.js` passes `source: "recoded"` when adding a code, so
`SELECT * FROM codes WHERE source='recoded'` answers "what changed during
recoding" later. Removing a code already soft-deletes it (`deleted_at`), which is
enough history on its own -- no separate audit table needed.

A model-flagged card also has "✕ Not this theme", which records an explicit rejection
(`POST /api/coding/negatives`, `source: "review_reject"`; see "Explicit rejections" above). The
card stays on screen with Undo until the queue reloads, after which `get_review_candidates()`
skips that segment for that theme. Every candidate carries `rejected_theme_ids` alongside
`theme_ids`.

**Queue modes (roadmap B1).** The Queues sub-tab's Queue selector (`#reviewMode`) picks one of the
per-theme queues from `GET /api/review/queue` (`coding_store/queues.py`), each over the theme's
latest run of the selected engine, paged 25 at a time: `top`, `uncertain` (nearest the
threshold), `boundary` (the near-miss hunt, CODEBOOK_PLAN K4: judged negatives from read-in-full interviews,
explicit rejections and "none of these", minus flagged near misses, highest `oof_score` first),
`audit` (undecided passages above threshold, shuffled with a seed of model_version +
theme_id so pages are stable within a run), `suspect` (decided passages ranked by how far their
`oof_score` disagrees with the label; needs a run with out-of-fold scores), and `confusion`
(coded here, at or above another theme's threshold in the same scheme, excluding the theme's own
lineage/subtree). "Decided" means effectively positive or a reviewed negative
(`labels.reviewed_negative_segment_ids()`). `confusion` only excludes *explicit* decisions for the
other theme (coded or rejected): a "fully coded" mark makes every coded passage implicitly "not
the other themes", which would empty the view in a marked project. `queues.order_queue()` is the
pure ordering rule, covered by `tests/test_queues.py`. Queue items reuse `renderReviewCard()` via
`queueItemToCandidate()`, which adds `judged` and `oof` reason pills. Measured on a real
389k-segment project: about 0.2 s per page, and 0.8 s for `confusion`.

**Card explanations (roadmap B3).** Every Review card (both views) and the Train tab's exemplars
highlight the relevant run's top positive terms, via `coding.js`'s `highlightTermsHtml()`: whole
words, case-insensitive, Unicode-aware, a bigram matching across punctuation, output HTML-escaped.
Queue pages carry `top_terms` (empty for embedding runs). The recode view fetches each selected
theme's terms via `GET /api/model/top_terms` and caches them per run. Review cards also show
per-theme score bars from `GET /api/review/segment_scores` (`model_runs.get_scores_for_segments()`:
each scheme theme's latest run of the selected engine, at most 300 segments per call), each with a
threshold tick and the theme's colour when above it.

**Triage decisions (roadmap B2).** In a queue, each card has Accept / Reject / Skip (keys `a` / `r` /
`s`, `j` / `k` to move, `u` to undo), sent to `POST /api/review/decisions`
(`coding_store/decisions.py`). Accept = `codes.add_code(source="review_accept")`, reject =
`codes.add_negative(source="review_reject")`, skip = no label change, in every mode. In
`confusion` the decision targets the *other* theme (`theme_id`), while `queue_theme_id` names the
queue. Each row in the new `review_decisions` table stores mode/engine/model_version/score/
oof_score and `prev_state` ("coded" | "rejected" | "none"). `undo()` restores that state by
reactivating the original code/rejection row (keeping its `created_at`/`source`) rather than
writing a new one, and only the latest active decision on a passage+theme can be undone. A round
is `round_id = model_version:queue_theme_id:mode`; `round_summary()` gives its active counts, shown
in the queue's summary line. Skips are recorded because roadmap C2's precision estimate from the
`audit` queue needs every passage shown. This table is the documented exception to "tag codes
instead of adding an audit table" (CLAUDE.md). Decisions are included in the codebook export.
Inside a queue, the per-pill "✕ Not this theme"/Undo buttons are hidden; the triage bar replaces
them, so every queue action is recorded. The recode view is unchanged. Tests:
`tests/test_decisions.py`.

## Web Appendix subsystem

The Web Appendix tab is a chronological log of research actions -- meant to be
citable methodological provenance a researcher can attach to a manuscript. It's backed by
two more `coding.db` tables: `activity_log` (append-only: timestamp, actor, action_type, a
free-form `details_json` blob) and `model_run_params` (one row per training pass, the
shared hyperparameters used, since `model_runs`' per-theme metrics say nothing about how
they were produced).

`coding_store/activity_log.py`'s `log_activity(actor, action_type, details)` is called from
every mutating action that previously had no actor/provenance at all: theme create/update/
archive/restore/merge (`coding_store/themes.py`'s theme functions each take an `actor` kwarg now),
`viewer_server.py`'s `run_train_job`/`run_query_job`, and `export_codebook`. This is
deliberately a *new*, cross-cutting table rather than adding actor columns to five
unrelated tables -- it doesn't replace `codes.coder`/`source`/`note`, which already covers
per-code provenance well and is untouched.

Browse filter state has nowhere to live server-side at all (purely client-side in
`app.js`'s `state.filters`), so it's captured as a `filter_snapshot` action only at
deliberate moments that "consume" the current filters -- a citation copy, a completed
download (`postFilterSnapshot()` in `app.js`) -- not on every filter change, which would
swamp the log with noise.

`coding_store/activity_log.py`'s `get_appendix_feed()` assembles `activity_log` with light joins (theme names
resolved from ids in `details_json`, `model_run_params` joined onto `training_run` rows)
for `GET /api/appendix/log`. `GET /api/appendix/export` renders the same feed through
`appendix_export.py` into a downloadable, self-contained HTML file.

## Project switching

The in-app **Project** picker (header button, `viewer_static/projects.js`) lets a researcher open,
create, or remove projects from the running dashboard -- no restart, no editing `.env`, aimed at
someone (e.g. a research assistant) less comfortable with the CLI than the `PROJECT_DIR` env var
requires.

`viewer_server.py`'s `switch_project(new_dir)` does the actual work: creates `new_dir` if it's new,
reassigns the module globals `QUERIES_DIR`/`EXPORTS_DIR` and calls the new
`coding_store.schema.set_db_path()` (reassigns `coding_store.schema.DB_PATH` and re-runs
`init_db()` against it -- creates a fresh schema for a new project, migrates an existing one),
evicts every `DATASETS` cache entry except `"primary"`/`"demo"` (those two stay repo-anchored
regardless of active project), then registers the new path in `project_registry.py`'s list.
Guarded by refusing to switch while `JOBS.has_running()` or `TRAIN_JOBS.has_running()` is true --
those background threads capture `QUERIES_DIR`/`coding_store.schema.DB_PATH` by module-level
name lookup at the time they run, so switching
underneath one risks it writing into the wrong project; the guard just refuses rather than trying
to handle that race.

The frontend does a full `window.location.reload()` after a successful switch rather than trying to
reset `state`/`codingState`/`modelState`/`reviewState` by hand -- simpler and guaranteed clean.

`project_registry.py` intentionally has no "active project" field of its own (see its module
docstring) -- each server process tracks that in its own memory
(`viewer_server.CURRENT_PROJECT_DIR`), since this app supports running more than one process at
once against different projects on different ports.

## Local development workflow

1. Put the JSON data file in the same folder as `viewer_server.py`, or update `DATA_FILE` in `viewer_server.py` to point to it.
2. Run `python viewer_server.py [port]`. The default port is 8765. Set `PROJECT_DIR` in `.env`
   first if you want this run to use a different `queries/`/`coding.db`/`exports/` location
   than the repo root (see `paths.py`) -- or just use the in-app Project picker once it's running.
3. Open `http://127.0.0.1:<port>/` in a browser.
4. After a code change to `viewer_server.py`, stop the server and start it again. It loads data only at startup.
5. After a change to a file in `viewer_static/`, reload the browser page. The server reads static files fresh on each request.
6. After each change, check the browser console for JavaScript errors.

Common data issues:

- A numeric field can arrive as a string, for example `"1200"` instead of `1200`. JavaScript's comparison operators convert strings to numbers in this case, so filters still work. Display code should still convert the value with `Number()` before it calls `toLocaleString()`.
- A date field can be missing or badly formed. The server's sort function already sends records with no date to the end of the list, instead of failing.

## Production guidance

This dashboard is built for one researcher on one machine, not for public or multi-user access. Before you expose it beyond your own machine, make these changes:

1. Add authentication. The server, as built, has none.
2. Run the server behind a reverse proxy, such as nginx, instead of exposing `ThreadingHTTPServer` directly to the internet.
3. Add HTTPS if the server is reachable outside a local machine or private network.
4. Move to a full web framework, such as Flask or FastAPI, if you need more than two endpoints, need request validation, or expect many concurrent users.
5. Keep secrets, such as API keys used to fetch source data, out of version control. Store them in a `.env` file and add that file to `.gitignore`. Commit a `.env.example` file with placeholder values instead.
6. Before you commit the data file or serve it from a shared machine, check that it does not hold information that should stay private.

## Known limits

- The server holds every dataset it has loaded in memory, in two forms: the raw record list and the index. Datasets load lazily (on first request) and stay cached for the life of the process, so browsing many large query exports in one session adds up. Before you run the server, confirm your machine has enough RAM.
- `coding.db` is a plain SQLite file, and SQLite was not built to be shared across machines through a cloud-sync client (Dropbox, OneDrive, Google Drive). If your project folder lives in one of those, do not have two people coding into the same `coding.db` from two different machines at the same time -- confirmed on this machine that a `.venv` sitting in a synced folder alone was enough to hit real, repeatable file-lock errors during setup (see `install.bat`'s comments); a live SQLite write conflict during sync is a real, untested risk, not a hypothetical one. If more than one person needs to code at once, have them share one running server (one machine, reached over the local network) instead of each running their own against a synced copy of the database.
- The server runs as a single process. `ThreadingHTTPServer` handles concurrent requests, but all requests share one in-memory copy of the data. Query/export jobs run in background threads so they don't block Browse, but two jobs running at once share the same rate-limited API key.
- `queries/` grows without bound — nothing deletes old exports automatically. Clean it out by hand if it gets large.
- The frontend has no automated tests. Test changes by hand in a browser.
- The frontend ships unminified. This is fine for local use and small teams. If load time in production matters, minify or bundle the files.

## Guidance for a Claude Code agent

1. Before you change `viewer_server.py` or `viewer_static/app.js`, read each file in full. Both files are short enough to read in one pass.
2. Keep the two-endpoint contract: a light index endpoint and a per-record detail endpoint. Do not merge them back into one large payload, since that removes the reason for this design.
3. Match field names exactly between the server's index payload and the frontend's filter code. A silent mismatch shows an empty filter list instead of an error.
4. Unless the user asks for a frontend framework or a build step, do not add one. The point of this pattern is that any agent can read and edit the whole frontend directly.
5. After a change, start the server and load the page in a real browser. Check the browser console for errors before you report the change as complete.

See [CLAUDE.md](../CLAUDE.md) at the repo root for the full, current version of this list
(it covers every tab and is kept as the canonical fast-reference; the five points above
are the original Browse/Search & Export-era version, left here for this section's history).
