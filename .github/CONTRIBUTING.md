# Contributing

This is a small, solo-researcher tool shared for other researchers to adapt to their own
qualitative-coding projects — not a framework with a public API to keep stable. Before changing
anything, read [CLAUDE.md](../CLAUDE.md)'s "Hard rules" section: it's the actual contribution
constraints (no frontend framework or build step, never change `segmentation.py`'s chunking
constants once real coding exists, never hard-delete themes or codes, keep the Browse
two-endpoint contract, etc.), kept in one place so it doesn't drift out of sync with a separate
contributing guide. [DEVELOPMENT.md](../docs/DEVELOPMENT.md) has the full technical detail behind each
subsystem.

In short:

- Read the whole file you're changing first — `viewer_server.py` and each `viewer_static/*.js`
  file are deliberately kept short enough to read in one sitting.
- No automated test suite exists by design (this is a two-person-at-most local tool) — after a
  change, run `python viewer_server.py` and exercise it by hand in a browser, checking the
  console for errors.
- Open an issue before a large or architectural change (new dependency, new subsystem, schema
  change to `coding_store.py`) so it can be discussed first.
