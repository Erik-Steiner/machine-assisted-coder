# CLAUDE.md

Project-level guidance for Claude Code agents working in this repo. Read this first; it's the
fast-orientation layer. [DEVELOPMENT.md](docs/DEVELOPMENT.md) has the full technical detail and is
the place to go before changing anything non-trivial. [README.md](README.md) is the human
setup/usage guide.

## What this is

A local, single-researcher web app for browsing a corpus of speaker-turn text — interview
transcripts, Reddit/forum threads, or other researcher-supplied data — and qualitatively coding
it. Seven tabs, one server (`viewer_server.py`):

- **Browse / Search & Export** — flat JSON datasets held in memory. Search & Export fetches new
  ones from the live ceointerviews.ai API via `query_api.py`; other sources come in via
  `scripts/import_*.py` (see [IMPORTING_DATA.md](docs/IMPORTING_DATA.md)).
- **Analytics** — read-only corpus-level scale/density stats (`corpus_analytics.py`) over the
  same segment set `classifier.py` trains on, plus a metadata breakdown and vocabulary-overlap
  view. No schema of its own.
- **Coding / Model / Review** — a codebook of researcher-defined themes applied to
  speaker-turn segments, backed by SQLite (`coding.db`). Model trains a TF-IDF + logistic
  regression classifier per theme (`classifier.py`); Review is a cross-interview recode queue.
- **Web Appendix** — a downloadable log of research actions (themes, training runs, imports,
  filter snapshots) for citing as manuscript supplementary material.

No frontend framework, no build step, no bundler. `viewer_static/*.js` files are loaded as
plain `<script>` tags in a fixed order (`app.js` → `coding.js` → `analytics.js` → `model.js` →
`review.js` → `appendix.js`), each one sharing the previous ones' global `state`/`codingState`/etc.
rather than duplicating navigation logic. This is deliberate — see "Hard rules" below.

The canonical record/segment field names are source-agnostic (`group_name`/`person_name`/
`item_id`/`turns`, not `company`/`executive`/`feed_item_id`/`enhanced_transcript_json`) — see
[IMPORTING_DATA.md](docs/IMPORTING_DATA.md) for the full schema. Search & Export's own UI/API code is
the one exception, since it legitimately still speaks ceointerviews.ai's own vocabulary
(`company_id`, `entity_id`, etc.) — a different, source-specific concept from the canonical
schema, not something to rename.

## Where to look, by task

| Working on...                          | Read first                                          |
|-----------------------------------------|------------------------------------------------------|
| Browse filters/rendering/citation       | `viewer_static/app.js`, `viewer_server.py`           |
| Search & Export / bulk download         | `query_api.py`, `download_script.py`                 |
| Importing a new data source             | [IMPORTING_DATA.md](docs/IMPORTING_DATA.md), `query_api.py`'s `EXPORT_FIELDS`/`validate_dataset_records`, `scripts/import_*.py` |
| Coding tab UI/keyboard shortcuts        | `viewer_static/coding.js`                            |
| Codebook / segments / codes data model  | `coding_store/schema.py` (schema), `coding_store/themes.py`, `coding_store/codes.py`, `segmentation.py` |
| Classifier training or metrics          | `classifier.py`, `scripts/train_classifiers.py`      |
| Corpus-level analytics / vocab overlap  | `corpus_analytics.py`, `viewer_static/analytics.js`  |
| Model tab UI                            | `viewer_static/model.js`                             |
| Review / recode queue                   | `viewer_static/review.js`, `coding_store/review.py`'s `get_review_candidates` |
| Web Appendix / activity log             | `coding_store/activity_log.py`'s `log_activity`/`get_appendix_feed`, `appendix_export.py`, `viewer_static/appendix.js` |
| Multi-project data locations            | `paths.py` (`PROJECT_DIR` env var), the in-app Project picker (`project_registry.py`, `viewer_server.switch_project`, `viewer_static/projects.js`) |
| Segmenting / duplicate-scan UI          | `viewer_server.py`'s `run_segment_job`/`run_duplicate_job`, `jobs.py`'s `JobRegistry`, `scripts/find_duplicates.py`'s `run_duplicate_scan`, `viewer_static/coding.js`'s Datasets panel |
| App setup / installer                   | `install.bat`/`start.bat` (Windows), `install.command`/`start.command` (macOS), `viewer_server.py`'s `main()` (port retry, `webbrowser.open()`) |
| Any endpoint                            | `viewer_server.py`'s module docstring — it's the complete, authoritative endpoint list |

## Hard rules

1. **Code is ground truth, not the docs.** If docs/DEVELOPMENT.md/README.md and the code disagree,
   the code wins — fix the docs, don't assume the docs describe intended-but-unbuilt behavior.
2. **No build step, no framework, unless the user explicitly asks for one.** The entire point
   of this frontend's design is that any agent can open a `.js` file and read it in one pass.
   Adding React/webpack/etc. as a "cleanup" is out of scope even if it looks like an improvement.
3. **Keep the two-endpoint contract in Browse.** A light `/api/index` (metadata only) and a
   per-record `/api/interview/<id>` (full record). Don't merge them back into one payload —
   that's the reason transcripts don't slow down filtering.
4. **Never change `segmentation.py`'s `TARGET_WORDS`/`MERGE_MIN`/`SPLIT_MAX`** once real coding
   exists in `coding.db`. `segment_id` is derived from `seg_index`; changing the chunking
   reshuffles ids and orphans every existing code. Treat any change here as a deliberate
   re-segmentation + migration, discussed with the user first, not a routine tweak.
   Renaming/adding a *column* is safer and has a precedent to follow: `coding_store/schema.py`'s
   `CURRENT_SCHEMA_VERSION`/`_migrate()` (bump the constant, add one idempotent
   `ALTER TABLE ...` block, same idiom as the existing `merged_into`/schema_version 2 checks) —
   `ALTER TABLE ... RENAME COLUMN`/`ADD COLUMN` don't touch row data, so they're safe even
   against the researcher's real `coding.db`. Verify against a copy first regardless.
5. **Don't hard-delete themes or codes.** Themes archive (`set_theme_status`) or merge
   (`merge_themes`); codes soft-delete (`deleted_at`). This is research provenance, not
   disposable state — the `coding_store` package has no delete path for either, and that's intentional.
6. **`coding.db` and `queries/` are not source-controlled and not disposable.** Both are
   gitignored (see `.gitignore`) because they're large/regenerable-except-for-actual-codes, but
   the researcher's real codebook and codes exist *only* in `coding.db`. Never delete, truncate,
   or bulk-modify it outside the documented API (the `coding_store` package's functions) without the user's
   explicit sign-off.
7. **Match field names exactly between server and frontend.** A mismatch between the index
   payload's keys and a frontend filter's field name fails silently (empty filter list), not
   loudly. Grep both sides after renaming a field — see [IMPORTING_DATA.md](docs/IMPORTING_DATA.md)
   for the canonical schema every dataset and every layer of the app is expected to share.
8. **No automated test suite exists**, with one deliberate, narrow exception: `tests/` holds
   `unittest`-based regression tests for `import_interview_transcript.py`'s pure line-parsing
   functions (`parse_turns`/`parse_labeled_turns`/`parse_transcript_turns`/`bridge_backchannels`)
   specifically, since they're cheap to test (no DB/server/file I/O) and the labeled-turn format
   is a tunable heuristic worth a regression net. Run with `python -m unittest discover -s tests`.
   This isn't a green light to add tests elsewhere by default — for everything else, after a
   change, start the server (`python viewer_server.py`) and exercise the actual feature in a
   browser; check the browser console for JS errors before calling a frontend change done. Don't
   claim a UI change works without having done this.
9. **Read both files in full before editing `viewer_server.py` or a `viewer_static/*.js`
   file.** They're each meant to be readable in one pass — skimming risks missing a shared
   hook (`window.onEnterCodingTab`, `codingState.pendingFocusSegmentId`, etc.) another tab
   depends on.
10. **Never store "the active project" as a shared field in `project_registry.py`'s file.**
    This app supports more than one `viewer_server.py` process running at once against
    different projects (see `paths.py`'s `PROJECT_DIR`) — a single shared "active" value would
    let one process's switch silently corrupt what a *different* running process's UI reports
    as active. "Which project is active" is always per-process state
    (`viewer_server.CURRENT_PROJECT_DIR`); the registry only ever holds the shared *list* of
    known project folders. This was a real bug caught during development, not a hypothetical.
11. **Never exercise, verify, smoke-test, or otherwise write through the app against the
    default project (this repo's own root) or any project a researcher has actually opened —
    not even a read-only-looking check, and not even "just this once" while verifying something
    else.** The researcher's real `coding.db`/`queries/` hold irreplaceable research data,
    per rule 6. Set `PROJECT_DIR` to a dedicated test project before starting
    `viewer_server.py` for any of that — `%LOCALAPPDATA%\InterviewViewer\test-project` on
    Windows is this repo's standing convention for it (create it if it doesn't exist yet; safe
    to reuse and to leave test data in, since nothing there is real). A throwaway temp
    directory is an acceptable substitute for a single one-off check, but prefer the standing
    test project so test runs stay easy to find and audit later. This applies to every agent
    working in this repo, not just the one that originally wrote a given feature — say so
    explicitly in any task handed to a subagent, since a subagent scoped to "verify your own
    change" has no way to know this rule unless told. This was a real incident, not a
    hypothetical: a subagent verifying unrelated work once wrote test themes (create/archive/
    merge) straight into the researcher's real `coding.db` because nobody had told it not to.

## Conventions worth matching

- New Python modules follow the existing docstring style: a short paragraph of *why*, not just
  *what*, especially for any non-obvious constant or invariant (see `segmentation.py`,
  `classifier.py` for examples).
- Background jobs (query fetch, classifier training) share one seam: `jobs.JobRegistry`
  (`start`/`mutate`/`update`/`get`/`has_running`) on the backend, `pollBackgroundJob()` in
  `app.js` on the frontend. Use these for a new long-running action rather than hand-rolling
  another `{job_id: {...}}` dict or another recursive `setTimeout` poll loop.
- `source`/`note`/`coder` fields on `codes` exist for provenance (e.g. `review.js` tags
  recode actions `source: "recoded"`). Prefer tagging new write paths this way over adding a
  parallel audit table.

## Known drift / forward-looking schema

`coding_store/schema.py`'s schema defines `negatives`, `embeddings`, `cluster_runs`, `clusters`,
`cluster_segments`, and `consistency_checks` tables for future phases (explicit negative
labeling, embeddings-based v2 classifier, topic clustering, inter-rater checks). They're created
by `init_db()` but nothing currently reads/writes most of them — don't assume a table's
existence means a feature is live; confirm there's a calling endpoint first.
