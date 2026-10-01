# Roadmap: supporting the full code → classify → iterate → embed → explore workflow

This is the agreed production plan (2026-10-01) for extending the app to fully support
the research workflow below. It records **decisions**, not just ideas -- each item says what
changes relative to the current code and why. Check items off (and note the commit) as they
land; if the code and this doc disagree, the code wins (CLAUDE.md rule 1) and this doc gets
fixed.

## The target workflow

1. **Upload** text data (transcripts, Reddit/forum threads, other speaker-turn text).
2. **Qualitatively code** it and identify 2+ themes of interest.
3. **Apply a TF-IDF classifier** to coded and uncoded segments.
4. **Use the preliminary model** to find more passages that fit each theme, and passages
   that are incorrectly labeled.
5. **Iterate** between 2–4, refining themes and building a labeled database, until there are
   enough labels to inform an embedding model.
6. **Apply an embedding / external model**, supervised by the codebook built in 1–5.
7. **Explore semantic structure**: clusters around each theme, how themes interact with
   metadata, more exemplar passages per theme.

## Where the app stood at the start (2026-09-30)

| Stage | Status | Main gaps |
|---|---|---|
| 1 Upload | Strong | -- |
| 2 Coding | Good core | No corpus-wide segment search; `codes.note` has no UI; no exemplar flag; shortcuts only for top-level themes 1–9 |
| 3 TF-IDF | Works | Trains every scheme at once; parent themes ignore sub-codes; every uncoded segment is a negative; out-of-fold scores discarded |
| 4 Find more / mislabels | Partial | Can't reject a prediction (`negatives` unused); top-30 only, no uncertainty/audit/suspect queues; "latest run" mixes engines |
| 5 Iterate | Partial | No learning curves, no per-round yield, no gold/holdout set |
| 6 Embedding / external | Minimal | One local model; export exists but no way to import external scores back |
| 7 Explore | Absent | `cluster_*` tables unused; no map, neighbors, semantic search, or theme × metadata view |

## Standing decisions

- **Priority:** stages 4–5 first (Phases A → B → C), then D, then E. F runs alongside.
- **Stage 6 scope:** keep the existing optional local-embeddings install and full-corpus
  export; add only a **round-trip import of external scores** (D). A local model picker, API
  embeddings, and an LLM few-shot classifier are deferred until users ask for them.
- **Negatives:** configurable per training run (`negative_mode`). Default `reviewed`
  (explicit rejects + reviewed-but-uncoded segments are negatives, and unread segments are
  unlabeled); `all_uncoded` keeps today's behavior.
- **Hierarchy:** parent themes roll up their descendants' codes as positives.
- **"Reviewed":** an interview-level "Mark fully coded" plus an optional per-segment `n` key
  ("none of these themes"). Both are timestamped; a mark only implies a negative for themes
  created before it.
- **Triage provenance:** a new `review_decisions` table (includes skips). Accepts still write a
  code (`source="review_accept"`) and rejects still write a negative. This is a deliberate
  exception to CLAUDE.md's "tag codes rather than add audit tables" convention, because skips
  and per-round precision can't live on a codes row.
- **Gold sample:** scheme-specific, coded blind, frozen, excluded from training. A random core
  (default 400) plus an optional score-band supplement, with each segment's chance of
  selection stored so estimates can be reweighted.
- **Tests:** narrow `unittest` coverage for label-building logic only (temporary `coding.db`
  per test), as a second named exception to CLAUDE.md rule 8.
- **General rules:** additive surfaces rather than changing existing views; heavy optional
  dependencies go in separate requirements files, are imported only when used, and are
  installed only from an in-app button; one schema bump (v4) using only `ADD COLUMN` /
  `CREATE TABLE IF NOT EXISTS`, verified on a copy of a real `coding.db` first; all app
  verification in `%LOCALAPPDATA%\InterviewViewer\test-project` (rule 11).

## Phase A — Foundations (M1, then A6 in M2)

Implementation order: **A3 → A4 → A7 → A1 → A5 → A2 → A6** (low-risk plumbing first, then
the changes that alter what the model learns). A7 follows A4 because "keep the latest run per
engine" depends on A4's engine-aware notion of "latest".

- [x] **A3 Persist out-of-fold scores.** *(Done 2026-10-01, schema v4.)* `predictions.oof_score` (v4). `train_theme()` already
  computes them for the CV metrics and then throws them away; the saved `score` comes from the
  final model, which trained on every coded segment, so a miscoded segment still scores high.
  Basis for the suspect-labels queue (B1) and Phase C.
- [x] **A4 Engine-aware latest run.** *(Done 2026-10-01.)* `get_latest_model_runs(engine=)`; engine selector in
  Review; cards show which engine/run produced them. Today an embedding run silently switches
  Review to mixed-engine scores.
- [x] **A7 Opt-in pruning of old runs' scores, and database compaction.** *(Done 2026-10-01.)*
  Every training pass writes one `predictions` row per in-scope segment for each trained theme,
  and old runs are never removed. The real project measured on 2026-10-01 (10 runs): 4.40M
  rows, most of a 1.24 GB `coding.db`, about 280 bytes per row including indexes. Training
  scores only in-scope segments (about 72k per theme, not all 389k), so that's roughly 18 MB
  per trained theme per run, about 90 MB for a 5-theme retrain. Measured on a copy: pruning
  to the latest 2 runs per theme took 160 s; compacting then took 7 s and shrank the file
  from 1,242 MB to 431 MB. One deviation from the plan: pins live in their own
  `model_run_pins` table (when, who, why) instead of a `model_run_params.pinned` column,
  because not every run has a params row and a pin's reason is worth recording.
  - **Scope:** deletes only per-segment `predictions` rows for runs the researcher selects.
    It **never** touches `codes`, `negatives`, `themes`, or a run's summary record
    (`model_runs` metrics, `model_top_terms`, `model_run_params`). Learning curves (C1) and
    the Web Appendix keep working for pruned runs.
  - **Always kept:** the latest run per theme per engine (as defined in A4), and any
    **pinned** run (`model_run_pins` table). Pin a run you cite in a paper or used for an
    export. Pruned scores can't be regenerated exactly, because the codes they were trained on
    have since changed.
  - **UI:** a Storage panel in the Model tab. It shows database size, rows and estimated MB per
    run, and which runs are pinned. It proposes a default "keep the latest N per theme/engine
    (N=2)" selection, and nothing is deleted until the researcher confirms. Never automatic,
    and never triggered by training.
  - **Compaction:** SQLite doesn't give space back to the disk after `DELETE`. A separate
    "Compact database" button runs `VACUUM` as a background job (`jobs.JobRegistry`). It
    warns that it needs free disk space up to the database's size, can take minutes on a
    large file, and blocks coding while it runs.
  - **Provenance:** each prune is logged to `activity_log` (versions, rows deleted, bytes
    reclaimed), so the Web Appendix records that a run's per-segment scores were pruned while
    its metrics remain.
  - **Constraints on later phases:** anything that needs a run's scores after it stops being
    latest must store its own summary at training time. C3's gold-sample metrics go in their
    own per-run table, not computed on the fly from `predictions`. D3 compares the latest run
    per engine, which is always kept.
  - **Possible follow-up (not in A7):** shrink each `predictions` row by replacing the
    repeated `model_version`/`theme_id` strings and the per-row `created_at` with a small
    integer run key. Probably a 2–3× reduction, but it's a real table rebuild, so it needs
    its own plan and sign-off (rule 6).
  - **Embeddings cache:** the Storage panel also lists the size of each `embeddings/*.npy`
    file. A model's file can be deleted and rebuilt, at CPU cost.
- [x] **A1 Scheme-scoped training.** *(Done 2026-10-01.)* `run_training_pass(scheme_id=)` (and the embedding pass,
  whose readiness check is scheme-scoped but whose training isn't). `scheme_id` logged in
  `model_run_params`. A "train all schemes" option stays.
- [x] **A5 Explicit negatives.** *(Done 2026-10-01, schema v5: also added `source`/`note` to rejections, matching codes.)* `negatives.deleted_at` (soft delete, rule 5);
  `POST /api/coding/negatives[/delete]`; a code and a rejection for the same
  segment/theme/coder cancel each other out. A basic "✕ not this theme" on Review's predicted
  cards (full triage UI in B2). Optional later: extra weight for explicit negatives.
- [x] **A2 Hierarchy roll-up.** *(Done 2026-10-01, `coding_store/labels.py`. Review's "Currently coded" list stays exact by design; archived sub-codes still roll up.)* Positives = own codes ∪ descendants' codes, computed at
  training time (nothing new written to `codes`). A rejection flows down to sub-codes, never
  up. Coverage/value-prop/`already_coded` use the same definition. `roll_up` logged. A
  faint "implied" mark on parent chips in Coding.
- [x] **A6 Reviewed state + `negative_mode`.** *(Done 2026-10-01: `coding_store/reviewed.py`, `labels.reviewed_negative_segment_ids()`. Plus a bulk-mark tool for already-coded interviews, dated as of each one's last code or a chosen date, with the real time kept in `item_reviews.recorded_at` (v6).)* `item_review_status` table and per-segment `n`
  confirmations, both timestamped. `reviewed` mode trains on positives + explicit rejects +
  uncoded segments reviewed *after the theme was created*; unread segments are scored but not
  trained on. Warn (and offer `all_uncoded`) below ~50 negatives. Mode logged per run.

## Phase B — Active-learning triage (stage 4)

- [x] **B1 Queue modes** *(Done 2026-10-01, `coding_store/queues.py`. `confusion` excludes only explicit decisions for the other theme, so "fully coded" marks don't empty it.)* — `GET /api/review/queue?theme_id&mode&engine&offset&limit`:
  `top`, `uncertain` (near threshold), `audit` (seeded random above threshold),
  `suspect` (coded with low OOF score; rejected with high OOF score), `confusion` (coded A,
  high for sibling B). Excludes segments already decided for that theme. Paged.
- [ ] **B2 Triage panel** in Review (the existing coded/predicted view stays). Keys:
  `a` accept / `r` reject / `s` skip / `u` undo / `j`,`k`. Every decision is written to
  `review_decisions`.
- [ ] **B3 Explanations:** highlight the theme's top terms in the card; per-theme score bars.
- [ ] **B4 Memos and codebook rigor:** editable `codes.note`; `themes.inclusion_criteria` /
  `exclusion_criteria`; `codes.is_exemplar`.
- [ ] **B5 Corpus-wide segment search** (FTS5, falling back to `LIKE`), results as codeable
  cards.
- [ ] **B6 Shortcuts** for sub-codes and themes past #9 (quick-pick palette).

## Phase C — Iteration tracking (stage 5)

- [ ] **C1 Learning curves** (labels vs. PR-AUC/F1 across runs), reusing `analytics.js`'s
  SVG helpers.
- [ ] **C2 Per-round stats** from `review_decisions`: accept rate by mode; `audit`-mode
  precision above threshold with a Wilson confidence interval.
- [ ] **C3 Gold validation sample** (`validation_samples`, `validation_sample_items` with
  each segment's chance of selection): blind coding mode with an explicit "none of these";
  excluded from training; reweighted precision/recall/prevalence.
- [ ] **C4 Readiness panel** per theme: positives, explicit negatives, gold-sample F1, PR-AUC
  plateau over the last 3 runs, audit precision. Advisory; the 150-positive gate stays.

## Phase D — Stage 6: external-score round trip

- [ ] **D1 Scoring pack export:** every in-scope segment with its text and metadata, codebook
  definitions and criteria, and labeled positives / rejects / gold. Format documented.
- [ ] **D2 Import external scores:** `POST /api/model/import_scores` (`segment_id, theme_id,
  score`, optional `threshold`/`label`/`rationale`) → model version `ext_v1_<name>_<ts>`, with
  the file's SHA-256 and a method note in `model_run_params`. If no threshold is supplied,
  pick the best-F1 one against the gold sample (or against codes, with a warning). Teach
  `engineOfVersion` and `topic_model_export._engine_prefix` the `ext` prefix.
- [ ] **D3 Engine comparison** per theme: CV metrics, gold-sample metrics, pairwise agreement
  at each engine's threshold.

## Phase E — Explore tab (stage 7)

- [ ] **E3 "More like this"** (cosine nearest neighbors over cached vectors) on every
  segment card; free-text semantic search when the local model is installed, otherwise the
  B5 keyword search.
- [ ] **E4 Theme × metadata:** prevalence (coded, or predicted by a chosen engine at its
  threshold) by group/person/time, per-segment normalized; theme co-occurrence matrix; CSV
  export.
- [ ] **E0 Import external embeddings** (`.npy`/CSV keyed by `segment_id`) into the
  embeddings cache.
- [ ] **E1 2D map:** PCA (core) or UMAP (optional `requirements-explore.txt`); canvas scatter
  colored by coded/predicted theme, metadata, or cluster; click → codeable card; lasso → a
  triage queue (never bulk-coding).
- [ ] **E2 Clustering:** `sklearn.cluster.HDBSCAN` / KMeans, corpus-wide or within one theme;
  writes the existing `cluster_runs`/`clusters`/`cluster_segments` tables; cluster labels from
  top terms and the passages nearest each centroid; "Promote to sub-code" → new child theme
  + triage queue.
- [ ] **E5 Most- and least-typical passages:** rank coded segments by closeness to their
  theme's centroid; the most typical become exemplar candidates, the outliers feed `suspect`.

## Phase F — Provenance (alongside each phase)

- [ ] Web Appendix action types: review-round summaries, `negative_mode`/roll-up settings,
  gold-sample creation, external-import hashes, cluster runs, promote-to-sub-code.
- [ ] Methods-summary export: codebook with criteria, label counts per theme by source,
  metrics per iteration including gold-sample metrics.

## Milestones

| | Contents |
|---|---|
| M1 | A3, A4, A7, A1, A5, A2 |
| M2 | A6, B1, B2 (the core active-learning loop) |
| M3 | B3–B6 |
| M4 | C1–C4 |
| M5 | D1–D3 |
| M6 | E3, E4, then E0, E1, E2, E5 |

## Schema changes (additive only)

- `predictions.oof_score REAL` (A3) — shipped
- `model_run_pins` table (A7) — shipped (additive table, no version bump needed)
- `negatives.deleted_at`, `source`, `note` (A5) — shipped as v5
- `item_reviews`, `segment_none_confirmations` tables (A6) — shipped (new tables, no bump)
- `item_reviews.recorded_at` (A6 bulk mark) — v6
- `review_decisions` (B2)
- `themes.inclusion_criteria`, `themes.exclusion_criteria`, `codes.is_exemplar` (B4)
- `segments_fts` FTS5 virtual table (B5)
- `validation_samples`, `validation_sample_items` (C3)

**v4 shipped to the real `coding.db` on 2026-10-01** (it migrated the next time the app ran),
so every column change still listed above needs a **v5** bump in `schema._migrate()`. New
tables (`CREATE TABLE IF NOT EXISTS`) need no bump.
