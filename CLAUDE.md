# CLAUDE.md

Project-level guidance for Claude Code agents working in this repo. Read this first; it's the
fast-orientation layer. [DEVELOPMENT.md](DEVELOPMENT.md) has the full technical detail and is
the place to go before changing anything non-trivial. [README.md](README.md) is the human
setup/usage guide.

## What this is

A local, single-researcher web app for browsing a corpus of speaker-turn text — interview
transcripts, Reddit/forum threads, or other researcher-supplied data — and qualitatively coding
it. Six tabs, one server (`viewer_server.py`):

- **Browse / Search & Export** — flat JSON datasets held in memory. Search & Export fetches new
  ones from the live ceointerviews.ai API via `query_api.py`; other sources come in via
  `scripts/import_*.py` (see [IMPORTING_DATA.md](IMPORTING_DATA.md)).
- **Coding / Model / Review** — a codebook of researcher-defined themes applied to
  speaker-turn segments, backed by SQLite (`coding.db`). Model trains a TF-IDF + logistic
  regression classifier per theme (`classifier.py`); Review is a cross-interview recode queue.
- **Web Appendix** — a downloadable log of research actions (themes, training runs, imports,
  filter snapshots) for citing as manuscript supplementary material.

No frontend framework, no build step, no bundler. `viewer_static/*.js` files are loaded as
plain `<script>` tags in a fixed order (`app.js` → `coding.js` → `model.js` → `review.js` →
`appendix.js`), each one sharing the previous ones' global `state`/`codingState`/etc. rather than
duplicating navigation logic. This is deliberate — see "Hard rules" below.

The canonical record/segment field names are source-agnostic (`group_name`/`person_name`/
`item_id`/`turns`, not `company`/`executive`/`feed_item_id`/`enhanced_transcript_json`) — see
[IMPORTING_DATA.md](IMPORTING_DATA.md) for the full schema. Search & Export's own UI/API code is
the one exception, since it legitimately still speaks ceointerviews.ai's own vocabulary
(`company_id`, `entity_id`, etc.) — a different, source-specific concept from the canonical
schema, not something to rename.

## Where to look, by task

| Working on...                          | Read first                                          |
|-----------------------------------------|------------------------------------------------------|
| Browse filters/rendering/citation       | `viewer_static/app.js`, `viewer_server.py`           |
| Search & Export / bulk download         | `query_api.py`, `download_script.py`                 |
| Importing a new data source             | [IMPORTING_DATA.md](IMPORTING_DATA.md), `query_api.py`'s `EXPORT_FIELDS`/`validate_dataset_records`, `scripts/import_*.py` |
| Coding tab UI/keyboard shortcuts        | `viewer_static/coding.js`                            |
| Codebook / segments / codes data model  | `coding_store.py` (schema at top), `segmentation.py` |
| Classifier training or metrics          | `classifier.py`, `scripts/train_classifiers.py`      |
| Model tab UI                            | `viewer_static/model.js`                             |
| Review / recode queue                   | `viewer_static/review.js`, `coding_store.get_review_candidates` |
| Web Appendix / activity log             | `coding_store.log_activity`/`get_appendix_feed`, `appendix_export.py`, `viewer_static/appendix.js` |
| Multi-project data locations            | `paths.py` (`PROJECT_DIR` env var), the in-app Project picker (`project_registry.py`, `viewer_server.switch_project`, `viewer_static/projects.js`) |
| Any endpoint                            | `viewer_server.py`'s module docstring — it's the complete, authoritative endpoint list |

## Hard rules

1. **Code is ground truth, not the docs.** If DEVELOPMENT.md/README.md and the code disagree,
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
   Renaming/adding a *column* is safer and has a precedent to follow: `coding_store.py`'s
   `CURRENT_SCHEMA_VERSION`/`_migrate()` (bump the constant, add one idempotent
   `ALTER TABLE ...` block, same idiom as the existing `merged_into`/schema_version 2 checks) —
   `ALTER TABLE ... RENAME COLUMN`/`ADD COLUMN` don't touch row data, so they're safe even
   against the researcher's real `coding.db`. Verify against a copy first regardless.
5. **Don't hard-delete themes or codes.** Themes archive (`set_theme_status`) or merge
   (`merge_themes`); codes soft-delete (`deleted_at`). This is research provenance, not
   disposable state — `coding_store.py` has no delete path for either, and that's intentional.
6. **`coding.db` and `queries/` are not source-controlled and not disposable.** Both are
   gitignored (see `.gitignore`) because they're large/regenerable-except-for-actual-codes, but
   the researcher's real codebook and codes exist *only* in `coding.db`. Never delete, truncate,
   or bulk-modify it outside the documented API (`coding_store.py` functions) without the user's
   explicit sign-off.
7. **Match field names exactly between server and frontend.** A mismatch between the index
   payload's keys and a frontend filter's field name fails silently (empty filter list), not
   loudly. Grep both sides after renaming a field — see [IMPORTING_DATA.md](IMPORTING_DATA.md)
   for the canonical schema every dataset and every layer of the app is expected to share.
8. **No automated test suite exists.** After a change, start the server
   (`python viewer_server.py`) and exercise the actual feature in a browser; check the browser
   console for JS errors before calling a frontend change done. Don't claim a UI change works
   without having done this.
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

## Conventions worth matching

- New Python modules follow the existing docstring style: a short paragraph of *why*, not just
  *what*, especially for any non-obvious constant or invariant (see `segmentation.py`,
  `classifier.py` for examples).
- Background jobs (query fetch, classifier training) follow the same shape: a `JOBS`-style dict
  keyed by `job_id`, a daemon thread, and a `GET .../status?job_id=` poll endpoint. Follow this
  pattern rather than inventing a new one if you add another long-running action.
- `source`/`note`/`coder` fields on `codes` exist for provenance (e.g. `review.js` tags
  recode actions `source: "recoded"`). Prefer tagging new write paths this way over adding a
  parallel audit table.

## Known drift / forward-looking schema

`coding_store.py`'s schema defines `negatives`, `embeddings`, `cluster_runs`, `clusters`,
`cluster_segments`, and `consistency_checks` tables for future phases (explicit negative
labeling, embeddings-based v2 classifier, topic clustering, inter-rater checks). They're created
by `init_db()` but nothing currently reads/writes most of them — don't assume a table's
existence means a feature is live; confirm there's a calling endpoint first.
