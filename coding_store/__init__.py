"""SQLite storage for the coding subsystem, split by concern -- each caller
imports the submodule it needs (`from coding_store import themes`), rather
than one flat module bundling segments/themes/codes/model_runs/review/
duplicates/dataset_status/activity_log together:

  schema.py        -- the SQLite schema, connection, and migration. Every
                       other submodule depends on this one; it depends on
                       none of them.
  segments.py       -- speaker-turn segments (scripts/build_segments.py's output).
  themes.py         -- the researcher's codebook: create/edit/archive/merge.
  codes.py          -- codes applied to segments.
  model_runs.py     -- Model tab storage: metrics, predictions, top terms.
  review.py         -- the Review tab's recode queue, and coding-progress counts.
  activity_log.py   -- Web Appendix: the research-action log, training-run
                       hyperparameters, and the codebook export.
  duplicates.py     -- near-duplicate detection (scripts/find_duplicates.py).
  dataset_status.py -- whole-dataset include/exclude/unload state.

This is the only persistent database in the app -- Browse/Search & Export
stay flat-JSON and in-memory, per DEVELOPMENT.md. See DEVELOPMENT.md for the
full reasoning behind this split.
"""
