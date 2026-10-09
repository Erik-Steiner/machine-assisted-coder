"""Local server for the qualitative-coding research webapp (interview
transcripts, Reddit/forum threads, or other researcher-supplied text --
see docs/IMPORTING_DATA.md).

Seven tabs, one server:

  Browse tab       — reads whichever dataset is selected and lets you page
                      through it. Every dataset lives in queries/, one JSON +
                      CSV pair per query, registered in queries/_index.json —
                      whether it was fetched by Search & Export below, by
                      download_script.py's bulk per-company runs, by one of
                      scripts/import_*.py, or by hand.
                      (If executive_interviews.json still exists at the repo
                      root from an older setup, it's also loaded at startup
                      as a one-off "primary" dataset, for backwards
                      compatibility. If data/demo_dataset.json exists -- a small
                      real sample bundled with the repo, see
                      scripts/make_demo_dataset.py -- it's loaded the same
                      way as a "demo" dataset, so a fresh clone has
                      something to browse with no API key or download.)
  Search & Export   — looks up a company or CEO/executive by name against the
                      live ceointerviews.ai API, downloads that person's or
                      company's transcripts, and saves them into queries/ as
                      a new dataset you can immediately switch to in Browse.
                      Also hosts the Import panel: upload your own interview
                      transcript (.docx/.txt/.json) or Arctic Shift export
                      (submissions/comments JSONL) with no command line
                      needed -- see the /api/import/transcript/* and
                      /api/import/reddit/* endpoints below,
                      scripts/import_interview_transcript.py and
                      scripts/import_reddit.py (the logic both share with
                      their CLI counterparts), and docs/IMPORTING_DATA.md.
  Coding            — a researcher builds a codebook of themes and codes
                      speaker-turn segments (precomputed by
                      scripts/build_segments.py from each item's `turns`)
                      against it. Backed by coding.db (SQLite), a separate
                      store from the in-memory JSON datasets above — see
                      the coding_store package. Data can come from the live API
                      (Search & Export) or from scripts/import_*.py for
                      interview transcripts and Reddit/Arctic Shift data --
                      see docs/IMPORTING_DATA.md.
  Model             — train/retrain a TF-IDF + logistic-regression classifier
                      per codebook theme (classifier.py), then monitor and
                      interpret it: metrics and their history across runs,
                      top predictive terms, and top-scored exemplar segments.
                      Read-only monitoring, no accept/reject-into-codebook
                      actions (that would be a future active-learning step).
  Review            — recode/refine an existing theme (or several at once):
                      a cross-interview queue of every segment already coded
                      under the selected theme(s), plus segments a trained
                      model top-scores for them but that aren't coded yet.
                      Recode actions are tagged source='recoded' so they're
                      distinguishable later from first-pass manual coding.
  Analytics         — read-only corpus-level scale/density stats
                      (corpus_analytics.py) over the exact same segment set
                      classifier.py trains on, plus a metadata breakdown and
                      vocabulary-overlap view. No schema of its own.
  Web Appendix      — a chronological log of research actions (theme CRUD,
                      training runs with their hyperparameters, downloads,
                      codebook exports, and Browse filter snapshots taken at
                      a citation/export moment), backed by coding_store.activity_log's
                      activity_log/model_run_params tables, downloadable as
                      a self-contained HTML file for a manuscript's
                      supplementary material. See coding_store.activity_log's
                      log_activity/get_appendix_feed and appendix_export.py.

Endpoints:

    GET  /api/datasets                    -> list of browsable datasets (registry-backed entries include
                                              "kind", e.g. "interview_import", so the transcript importer
                                              can offer only those as append targets), each carrying a
                                              "status": "active" | "excluded" -- "unloaded" datasets are
                                              dropped from this list entirely (see /api/datasets/all)
    GET  /api/datasets/all                -> same as /api/datasets but including "unloaded" ones too --
                                              the only way to find and restore one; backs the Coding tab's
                                              Datasets panel
    POST /api/datasets/status             -> {dataset_id, status: "active"|"excluded"|"unloaded", note?}
                                              -> flags a whole dataset ("corpus") as excluded from
                                              classifier training (still fully visible/codeable) or
                                              unloaded (hidden from Browse/Coding/analysis entirely, but
                                              nothing on disk or in coding.db is touched/deleted --
                                              restorable any time); "active" clears any existing flag. See
                                              coding_store.dataset_status / classifier.build_corpus()
    GET  /api/index?dataset=<id>          -> metadata-only records for one dataset
    GET  /api/interview/<id>?dataset=<id> -> full record (incl. transcript_text)

    GET  /api/search/companies?keyword=.. -> live get_companies lookup
    GET  /api/search/entities?keyword=..  -> live get_entities lookup
    POST /api/query/start                 -> kick off a background fetch+export job
    GET  /api/query/status?job_id=..      -> poll a job's progress/result

    GET  /api/transcript_search?dataset=<id>&q=<term>
        -> ids of records in that dataset whose transcript_text contains
           <term> (case-insensitive). transcript_text isn't in the light
           index payload, so this is how the frontend's "Interview contains"
           filter works without loading every transcript into the browser.

    GET  /api/schemes                                -> list coding schemes (independent codebooks)
    POST /api/schemes                                -> create a scheme
    POST /api/schemes/<id>/update                    -> edit a scheme's fields
    POST /api/schemes/<id>/archive                   -> soft-hide a scheme
    POST /api/schemes/<id>/restore                   -> un-archive a scheme

    GET  /api/codebook/themes?scheme_id=<id>         -> list one scheme's themes (flat, with
                                                         parent_theme_id/depth for the frontend to
                                                         render as a tree)
    POST /api/codebook/themes                        -> create a theme (scheme_id required,
                                                         parent_theme_id optional -- a hierarchical
                                                         sub-code under another theme in the same scheme)
    POST /api/codebook/themes/<id>/update            -> edit a theme's fields (name/description/
                                                         example_words/color/parent_theme_id/sort_order/
                                                         inclusion_criteria/exclusion_criteria/boundary_notes)
    POST /api/codebook/flag                          -> {segment_id, theme_id, kind: exemplar|near_miss,
                                                         flagged: bool, note?} -> flag/unflag the active code
                                                         (exemplar) or rejection (near miss); 400 if there's
                                                         none. Returns twins: already-flagged look-alikes
                                                         (a warning only) -- see coding_store/examples.py
    GET  /api/codebook/examples?theme_id=<id>         -> {exemplars, near_misses}: the theme's flagged
                                                         passages with notes and twin_of links
    GET  /api/codebook/overview?scheme_id=<id>        -> every active theme with its codebook fields,
                                                         counts (positives, exemplars, near misses, read-in-
                                                         full interviews with a positive) and completeness
                                                         checks -- the Train tab's Codebook sub-page; see
                                                         coding_store/codebook.py (advisory, never blocking)
    POST /api/codebook/themes/<id>/archive           -> soft-hide a theme
    POST /api/codebook/themes/<id>/restore           -> un-archive a theme
    POST /api/codebook/themes/<id>/merge_into         -> merge a theme's codes into another theme in
                                                         the same scheme, archive it

    GET  /api/coding/segments?item_id=<id>&scheme_id=<id>  -> ordered segments + their codes (theme_ids)
                                                         and active rejections (rejected_theme_ids); with
                                                         scheme_id, also each segment's none_confirmed flag
                                                         and the item's "fully coded" mark (item_review)
    POST /api/coding/item_review                     -> {dataset_id, item_id, scheme_id, reviewed: bool} ->
                                                         mark/unmark an item "fully coded" for a scheme: its
                                                         uncoded segments become judged negatives for themes
                                                         that existed at that moment (coding_store/reviewed.py,
                                                         labels.reviewed_negative_segment_ids())
    POST /api/coding/item_review/confirm             -> {dataset_id, item_id, scheme_id, confirmed: bool} ->
                                                         confirm (or withdraw) that a marked interview was read
                                                         in full; never changes marked_at (CODEBOOK_PLAN K5)
    GET  /api/coding/unconfirmed_marks?scheme_id=<id> -> active marks that don't count as read in full yet
                                                         (bulk_unconfirmed / unknown), with coded_segments,
                                                         inferred_by_coverage and display labels
    GET  /api/coding/backfill_candidates?scheme_id=<id> -> interviews with codes in the scheme but no
                                                         "fully coded" mark yet, with code counts, last
                                                         coding time, and display labels
    POST /api/coding/item_reviews/bulk               -> {scheme_id, items: [{dataset_id, item_id}], as_of:
                                                         "last_code" | ISO date-time} -> mark them all
                                                         fully coded, dated as of each item's last code or
                                                         the given moment (recorded_at = now) -- see
                                                         coding_store/reviewed.py's bulk_mark_items()
    POST /api/coding/segment_none                    -> {segment_id, scheme_id, confirmed: bool} -> confirm/
                                                         withdraw "none of this scheme's themes apply here"
    POST /api/coding/codes                           -> apply a code to a segment (also cancels the
                                                         same coder's rejection of that theme there)
    POST /api/coding/codes/delete                    -> remove a code from a segment
    POST /api/coding/negatives                       -> {segment_id, theme_id, source?, note?} -> reject
                                                         a theme for a segment ("this isn't that theme");
                                                         cancels the same coder's code for it there. Keeps
                                                         the segment out of future review queues -- see
                                                         coding_store/codes.py's add_negative()
    POST /api/coding/negatives/delete                -> undo a rejection (soft delete)
    POST /api/coding/segment_datasets                -> {dataset_ids?: [...]} -> kick off a background
                                                         job that segments the given datasets into
                                                         coding.db (or, if dataset_ids is omitted, every
                                                         dataset with zero segments so far) -- the in-app
                                                         equivalent of scripts/build_segments.py, except
                                                         it can also reach "primary"/"demo" (see
                                                         segment_new_dataset()/run_segment_job())
    GET  /api/coding/segment_status?job_id=..        -> poll a segment_datasets job's progress/result
    GET  /api/coding/progress?dataset=<id>&scheme_id=<id>  -> coding counts by theme/group, optionally
                                                         scoped to one scheme's themes
    GET  /api/review/queue?theme_id=<id>&mode=<m>&engine=<tfidf>&offset=<0>&limit=<25>
        -> one page of a review queue for one theme, from its latest run of `engine`.
           mode: top (highest undecided) | uncertain (nearest the threshold) | audit
           (fixed random sample above threshold) | suspect (decided passages the
           out-of-fold score disagrees with) | boundary (judged negatives from
           read-in-full interviews, rejections and "none of these", highest out-of-fold
           first: the near-miss hunt) | confusion (coded here, high for another theme in
           the scheme). {model_version, threshold, total, items} -- see
           coding_store/queues.py
    GET  /api/review/segment_scores?segment_ids=<a,b>&scheme_id=<id>&engine=<tfidf>
        -> {segment_id: {theme_id: {score, threshold, model_version}}} -- each scheme theme's
           score for each segment from its latest run of `engine` (at most 300 segments per
           call); backs the Review cards' per-theme score bars
    GET  /api/model/top_terms?theme_id=<id>&model_version=<v>  -> {positive, negative} terms of
                                                         one run (empty for embedding runs)
    POST /api/review/decisions                       -> {segment_id, theme_id, decision: accept|reject|skip,
                                                         mode, queue_theme_id, engine?, model_version?,
                                                         score?, oof_score?, flag?: exemplar (with accept) |
                                                         near_miss (with reject)} -> apply (accept = code it,
                                                         reject = record a rejection, skip = nothing) and
                                                         record a triage decision; returns it plus the
                                                         round's counts -- see coding_store/decisions.py
    POST /api/review/decisions/undo                  -> {decision_id} -> restore the passage's previous
                                                         state for that theme (its original code/rejection
                                                         row, provenance intact) and mark the decision undone
    GET  /api/review/round_summary?model_version=&theme_id=&mode=  -> {accept, reject, skip, total}
                                                         for one queue round
    GET  /api/coding/model_runs?engine=<tfidf|embed>  -> latest classifier metrics per theme, each
                                                         row tagged with its "engine"; engine (optional)
                                                         restricts "latest" to that engine's runs --
                                                         pass it wherever scores get shown, so an
                                                         embedding run never silently replaces TF-IDF
    GET  /api/coding/model_runs?theme_id=<id>        -> full model_runs history for one theme
    GET  /api/coding/review_candidates?theme_ids=<a,b>&predicted_limit=<30>&engine=<tfidf>
        -> recode queue: segments coded under theme(s) a/b, plus each theme's
           top-scored not-yet-coded predictions from its latest run of `engine`
           (default "tfidf"; never mixed across engines) -- see
           coding_store.review.get_review_candidates

    POST /api/model/train                            -> {scheme_id?, negative_mode?: "reviewed"|"read_in_full"|
                                                         "all_uncoded"}
                                                         (default "reviewed" -- see classifier.NEGATIVE_MODES)
                                                         -> kick off a background training
                                                         pass over that scheme's eligible themes (the Model
                                                         tab sends the active scheme), or every scheme's if
                                                         scheme_id is omitted; one shared model_version
    GET  /api/model/train_status?job_id=..           -> poll a training job's progress/result
    POST /api/model/install_embeddings_deps           -> kick off a background `pip install -r
                                                         requirements-embeddings.txt` (400 if one's
                                                         already running) -- lets a researcher enable
                                                         the embedding engine with a button instead
                                                         of a terminal command
    GET  /api/model/install_embeddings_deps_status?job_id=..  -> poll that install job
    GET  /api/model/embedding_readiness?scheme_id=<id>  -> {installed, volume_ready, ready, ...} --
                                                         whether the optional embedding-based
                                                         engine can be bootstrapped right now
                                                         (see requirements-embeddings.txt)
    POST /api/model/train_embeddings                 -> {scheme_id?, negative_mode?} -> kick off a background embedding-
                                                         training pass, scheme-scoped like /api/model/train
                                                         (400 if embedding_readiness() isn't met for that
                                                         scope); reuses the TF-IDF pass's training
                                                         pipeline with sentence embeddings instead
                                                         of TF-IDF features -- see classifier.py's
                                                         run_embedding_training_pass()
    GET  /api/model/train_embeddings_status?job_id=..  -> poll an embedding-training job
    GET  /api/model/interpret?theme_id=<id>          -> metrics + top terms + exemplar segments +
                                                         value-prop stat + full-corpus coverage
                                                         for a theme's latest model (see
                                                         classifier.py / scripts/train_classifiers.py)
    GET  /api/engines                                -> {score_engines: [{id, label, prefix, kind}], topic_methods: []}
                                                         -- the one list of engines the UI reads instead of
                                                         hard-coding it (CODEBOOK_PLAN L0); topic methods
                                                         (LLM, later embedding clustering) join it with Part 2
    GET  /api/runs?scheme_id=<id>                     -> {current_codebook_version, runs: [...]} -- every training
                                                         pass that trained one of the scheme's themes, newest
                                                         first, with engine, negative mode, the codebook
                                                         version it was trained under (null if not recorded),
                                                         pin, per-theme metrics and their means: the Train
                                                         tab's Runs page (CODEBOOK_PLAN U2; coding_store/runs.py)
    GET  /api/iteration/status?scheme_id=<id>         -> {round, steps: {codebook, train, review, decide: done|current|
                                                         stale|todo}, reasons: [{code, step, text, target: {tab,
                                                         subpage}}], latest_run} -- where the scheme stands in the
                                                         codebook -> train -> review -> decide cycle, for the cycle
                                                         strip on Train and Review (CODEBOOK_PLAN U3;
                                                         coding_store/iteration.py). Read-only, advisory
    GET  /api/model/export_full_corpus?scheme_id=&model_version=&engine=
        -> writes segment_topic_scores/topic_seed_pack (every segment's score against every
           trained theme, plus each theme's top terms + exemplars) to exports/ as JSON+CSV --
           the full-corpus topic-model artifact for a separate, unrelated pipeline (comparison,
           or seeding a different method); see topic_model_export.py

    GET  /api/storage/report?keep_latest=<2>          -> coding.db file sizes, reusable free space, and every
                                                         training pass with per-theme row counts, estimated
                                                         size, and prune status (latest/recent/pinned/
                                                         prunable/pruned) -- see coding_store/storage.py.
                                                         Read-only
    POST /api/storage/pin                             -> {model_version, pinned: bool, note?} -> pin/unpin a
                                                         training pass (pinned runs are never pruned)
    POST /api/storage/prune                           -> {pairs: [{model_version, theme_id}], keep_latest}
                                                         -> background job deleting those runs' per-segment
                                                         scores only (never codes or run summaries). Every
                                                         pair is re-checked against the prune rule first; one
                                                         protected pair refuses the whole request (400)
    GET  /api/storage/prune_status?job_id=..          -> poll a prune job
    GET  /api/storage/compact_check                   -> {ok, required_bytes, available_bytes, ...} -- whether
                                                         there's enough free disk space to compact
    POST /api/storage/compact                         -> background VACUUM + WAL truncate, returning freed space
                                                         to the disk (400 if disk space is short, or training/
                                                         pruning is running)
    GET  /api/storage/compact_status?job_id=..        -> poll a compact job

    GET  /api/export/codebook?scheme_id=<id>          -> writes schemes/themes/codes/model_runs to
                                                         exports/ as JSON+CSV (backup / publication
                                                         supplement); scheme_id optionally limits the
                                                         themes/codes export to one scheme
    GET  /api/export/codebook_themes?scheme_id=<id>   -> writes codebook_topic_themes.json (definitions, criteria,
                                                         researcher-flagged exemplars / near misses with twins
                                                         collapsed, completeness, codebook_version) and
                                                         codebook_labels.csv (every known pos/neg label per segment
                                                         and theme, with its source) to exports/ -- the codebook
                                                         for an external model (CODEBOOK_PLAN K7); see
                                                         codebook_export.py. {exported_to, files, codebook_version,
                                                         label_rows, completeness: [{theme_id, name, status, score,
                                                         total, missing}]}. Never blocked by completeness
    GET  /api/appendix/log?action_type=<t>&since=<ts>  -> the assembled research-action log
    POST /api/appendix/filter_snapshot                -> log the current Browse filter state
    GET  /api/appendix/export                        -> download the log as a self-contained HTML file

    GET  /api/projects                                -> known projects (project_registry.py) + which is active
    POST /api/projects/open                           -> {path, name} -> live-switch the active project (creates
                                                         the folder if new), no server restart -- see switch_project()
    POST /api/projects/remove                         -> {path} -> forget a project (registry only, never touches its data)
    POST /api/projects/browse_folder                  -> pops a native OS folder-picker dialog, returns the chosen path

    POST /api/import/transcript/parse                 -> {filename, content_base64} -> parses an uploaded
                                                         .docx/.txt/.json interview transcript (in memory, nothing
                                                         written yet) and returns a preview: speaker labels found,
                                                         which of the two auto-detected docx/txt formats matched
                                                         ("format_detected": "timestamped"|"labeled" -- see
                                                         parse_transcript_turns()), or record count (json) -- see
                                                         scripts/import_interview_transcript.py, the Search & Export
                                                         tab's Import panel
    POST /api/import/transcript/commit                -> {upload_token, ..., target_dataset_id?} -> finishes a
                                                         previously-parsed upload (role assignments + interview
                                                         metadata for docx/txt; nothing extra needed for json),
                                                         writes it into queries/, and segments it into coding.db
                                                         immediately (see segment_new_dataset()) -- ready for the
                                                         Coding tab with no separate scripts/build_segments.py
                                                         step, unlike every other import path in this file.
                                                         target_dataset_id is optional: if given (an existing
                                                         dataset registered with kind="interview_import" -- see
                                                         GET /api/datasets), the new interview(s) are appended
                                                         into that dataset instead of creating a new one-interview
                                                         dataset (see query_api.append_to_dataset())

    POST /api/import/raw_upload?filename=<name>        -> raw request body (NOT JSON -- the whole point is
                                                         avoiding a base64-in-JSON copy for a large file) ->
                                                         {upload_id}. Used ahead of /api/import/reddit/parse so
                                                         a multi-hundred-MB Arctic Shift comments export gets
                                                         streamed straight from the File object to the network,
                                                         rather than built up as a base64 JS string first (see
                                                         RAW_UPLOADS above for why that OOMs the browser tab).
    POST /api/import/reddit/parse                     -> {submissions_filename, submissions_upload_id,
                                                         comments_filename?, comments_upload_id?} -> parses a
                                                         previously raw_upload'ed Arctic Shift submissions(+comments)
                                                         JSONL export (in memory, nothing written yet) and
                                                         returns a preview: item/turn counts, subreddits found,
                                                         orphaned-comment count -- see scripts/import_reddit.py,
                                                         the Search & Export tab's Import panel
    POST /api/import/reddit/commit                    -> {upload_token, target_dataset_id?} -> finishes a
                                                         previously-parsed upload (no role-assignment input needed
                                                         -- Reddit data has no interviewer/respondent decision to
                                                         make), writes it into queries/, and segments it into
                                                         coding.db immediately, same as /api/import/transcript/commit.
                                                         target_dataset_id is optional: if given (an existing
                                                         dataset registered with kind="reddit_import" -- see
                                                         GET /api/datasets), the new submission(s) are appended
                                                         into that dataset instead of creating a new one (see
                                                         query_api.append_to_dataset())

    POST /api/duplicates/scan                         -> {dataset_ids?: [...]} -> kick off a background
                                                         near-duplicate detection run (or, if dataset_ids
                                                         is omitted, every dataset in queries/_index.json)
                                                         -- the in-app equivalent of scripts/find_duplicates.py
    GET  /api/duplicates/scan_status?job_id=..        -> poll a duplicates/scan job's progress/result
    GET  /api/duplicates/status?dataset=<id>          -> {item_id: {kind: "duplicate", is_canonical,
                                                         canonical_dataset_id, canonical_item_id, size,
                                                         needs_attention, source: "auto"|"manual"}
                                                         | {kind: "excluded"}} -- the latest
                                                         scripts/find_duplicates.py run's clusters, with any
                                                         researcher duplicate_overrides applied ({} if
                                                         neither exists yet) -- backs the Browse/Coding tabs'
                                                         "duplicate of ..." flag
    POST /api/duplicates/override                     -> {dataset_id, item_id, action: "exclude"|"include",
                                                         canonical_dataset_id?, canonical_item_id?} -> records
                                                         a manual correction (see
                                                         coding_store.add_duplicate_override) -- "exclude"
                                                         says this item is NOT a duplicate (overrides a false
                                                         positive); "include" says it IS a duplicate of the
                                                         given canonical item (a false negative, or a pair
                                                         the detector never compared). Always wins over the
                                                         automated run; never touches codes or queries/*.json
    POST /api/duplicates/override/remove               -> {dataset_id, item_id} -> clears a manual
                                                         override, reverting to whatever the automated run
                                                         says (or "not a duplicate")

    GET  /api/analytics/corpus/summary?field=<f>&value=<v>  -> the six corpus-level scale/density metrics
                                                         (total_documents, total_segments, total_word_count,
                                                         vocabulary_size, lexical_diversity, segment-length
                                                         spread) over the same segment set
                                                         classifier.build_corpus() trains on -- see
                                                         corpus_analytics.py. field/value (both optional)
                                                         restrict to documents where that metadata field
                                                         equals value (the metadata-breakdown drill-down)
    GET  /api/analytics/corpus/fields                 -> breakdown fields discovered from the current
                                                         corpus's document metadata (never hardcoded), each
                                                         with its detected type (categorical/temporal/
                                                         quantitative) and value/missing counts -- feeds the
                                                         Analytics tab's field selector
    GET  /api/analytics/corpus/breakdown?field=<f>    -> per-value/bin/point distribution of that field
                                                         (grouped bars for categorical, time-binned for
                                                         temporal, per-document scatter points for
                                                         quantitative) -- see corpus_analytics.field_breakdown
    GET  /api/analytics/corpus/vocab_overlap?field=<f> -> categorical fields only (400 otherwise) --
                                                         per-value shared-vs-unique vocabulary percentages
                                                         and each value's top unique terms, using the same
                                                         preprocessing as classifier.py's TfidfVectorizer

Run:
    python viewer_server.py [port]
Then open http://127.0.0.1:<port>/ in a browser (default port 8765).
"""
import base64
import binascii
import json
import os
import subprocess
import sys
import threading
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import appendix_export
import classifier
import codebook_export
import corpus_analytics
from coding_store import (
    activity_log,
    codebook,
    codes as codes_store,
    dataset_status as dataset_status_store,
    decisions as decisions_store,
    examples as examples_store,
    duplicates,
    iteration,
    model_runs,
    queues,
    review,
    reviewed as reviewed_store,
    runs as runs_store,
    schema,
    schemes,
    segments as segments_store,
    storage,
    themes,
)
import paths as project_paths
import project_registry
import query_api
import scripts.find_duplicates as duplicate_scanner
import scripts.import_interview_transcript as interview_importer
import scripts.import_reddit as reddit_importer
import segmentation
import topic_model_export
from jobs import JobRegistry
from routing import ApiError, DownloadResponse, FileResponse, JsonResponse, Router, write_response

HERE = Path(__file__).parent
DATA_FILE = HERE / "executive_interviews.json"
DEMO_FILE = HERE / "data" / "demo_dataset.json"
STATIC_DIR = HERE / "viewer_static"
QUERIES_DIR = project_paths.QUERIES_DIR
EXPORTS_DIR = project_paths.EXPORTS_DIR
# This process's own active project -- deliberately NOT read from
# project_registry.py, since more than one server process can run at once
# against different projects (see paths.py's PROJECT_DIR); "active" only
# makes sense per-process. switch_project() reassigns this.
CURRENT_PROJECT_DIR = project_paths.PROJECT_DIR

CODER_NAME = os.getenv("CODER_NAME", schema.DEFAULT_CODER)

schema.init_db()
project_registry.ensure_initialized(str(project_paths.PROJECT_DIR))


def _sort_key(rec):
    # Records without a publish_date sort last instead of raising/crashing.
    pd = rec.get("publish_date")
    return (1, "") if not pd else (0, pd)


def build_dataset(records, label, dataset_id, created_at, source):
    records = sorted(records, key=_sort_key)
    index = []
    by_id = {}
    for i, rec in enumerate(records):
        rec = dict(rec)
        rec.pop("turns", None)  # not used by the viewer
        rec["id"] = i
        by_id[i] = rec

        meta = dict(rec)
        meta.pop("transcript_text", None)  # keep the index payload light
        index.append(meta)
    return {
        "id": dataset_id,
        "label": label,
        "source": source,
        "created_at": created_at,
        "count": len(records),
        "index": index,
        "by_id": by_id,
    }


def load_primary_dataset():
    if not DATA_FILE.exists():
        return None
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            records = json.load(f)
        query_api.validate_dataset_records(records, DATA_FILE.name)
    except (json.JSONDecodeError, query_api.DatasetValidationError) as exc:
        print(f"Skipping {DATA_FILE.name}: {exc}")
        return None
    created_at = datetime.fromtimestamp(DATA_FILE.stat().st_mtime, tz=timezone.utc).isoformat()
    return build_dataset(records, DATA_FILE.name, "primary", created_at, "primary")


def load_demo_dataset():
    """A small real sample (see scripts/make_demo_dataset.py) bundled with the repo so a
    fresh clone has something to browse immediately, with no API key or download needed."""
    if not DEMO_FILE.exists():
        return None
    try:
        with open(DEMO_FILE, encoding="utf-8") as f:
            records = json.load(f)
        query_api.validate_dataset_records(records, DEMO_FILE.name)
    except (json.JSONDecodeError, query_api.DatasetValidationError) as exc:
        print(f"Skipping {DEMO_FILE.name}: {exc}")
        return None
    created_at = datetime.fromtimestamp(DEMO_FILE.stat().st_mtime, tz=timezone.utc).isoformat()
    return build_dataset(records, "Demo sample (10 interviews)", "demo", created_at, "demo")


DATASETS = {}
DATASETS_LOCK = threading.Lock()

_primary = load_primary_dataset()
if _primary:
    DATASETS["primary"] = _primary
    print(f"Loaded {_primary['count']} interviews from {DATA_FILE.name}")
else:
    print(f"No {DATA_FILE.name} found yet — Browse will be empty until you "
          f"run a query in Search & Export, or run download_script.py")

_demo = load_demo_dataset()
if _demo:
    DATASETS["demo"] = _demo
    print(f"Loaded demo dataset ({_demo['count']} interviews) from {DEMO_FILE.name}")


def load_query_registry():
    return query_api.load_registry(QUERIES_DIR)


def save_query_registry(entries):
    query_api.save_registry(QUERIES_DIR, entries)


def get_dataset(dataset_id):
    """Return a loaded dataset dict, loading a saved query from disk on first access.
    Returns None -- same as "doesn't exist" -- for a dataset flagged 'unloaded' in
    coding_store.dataset_status, so every caller's existing "unknown dataset" 404
    handling makes it disappear from Browse/Coding/etc. for free."""
    status = dataset_status_store.get_dataset_status(dataset_id)
    if status and status["status"] == "unloaded":
        return None
    with DATASETS_LOCK:
        cached = DATASETS.get(dataset_id)
    if cached:
        return cached
    if dataset_id == "primary":
        return None

    entry = next((e for e in load_query_registry() if e["id"] == dataset_id), None)
    if entry is None:
        return None
    json_path = QUERIES_DIR / entry["json_file"]
    if not json_path.exists():
        return None
    try:
        with open(json_path, encoding="utf-8") as f:
            records = json.load(f)
        query_api.validate_dataset_records(records, entry["json_file"])
    except (json.JSONDecodeError, query_api.DatasetValidationError) as exc:
        print(f"Dataset '{dataset_id}' failed to load: {exc}")
        return None
    dataset = build_dataset(records, entry["label"], dataset_id, entry["created_at"], "query")
    with DATASETS_LOCK:
        DATASETS[dataset_id] = dataset
    return dataset


def _all_datasets():
    """Every known dataset (primary/demo/query-registry entries), each carrying a
    "status" merged in from coding_store.dataset_status ("active" by default).
    Includes 'unloaded' ones -- list_datasets() below is the filtered view most of
    the app actually wants; this is for the Coding tab's Datasets panel, the only
    place an unloaded dataset can be found and restored."""
    statuses = dataset_status_store.list_dataset_statuses()
    out = []
    with DATASETS_LOCK:
        primary_ds = DATASETS.get("primary")
        demo_ds = DATASETS.get("demo")
    if primary_ds:
        out.append({
            "id": "primary", "label": primary_ds["label"], "count": primary_ds["count"],
            "source": "primary", "created_at": primary_ds["created_at"],
        })
    if demo_ds:
        out.append({
            "id": "demo", "label": demo_ds["label"], "count": demo_ds["count"],
            "source": "demo", "created_at": demo_ds["created_at"],
        })
    for entry in load_query_registry():
        out.append({
            "id": entry["id"], "label": entry["label"], "count": entry["count"],
            "source": "query", "created_at": entry["created_at"], "kind": entry.get("kind"),
        })
    for d in out:
        d["status"] = statuses.get(d["id"], {}).get("status", "active")
        d["segment_count"] = segments_store.count_segments(dataset_id=d["id"])
    return out


def list_datasets():
    """_all_datasets(), minus anything flagged 'unloaded' -- what the top-bar
    dataset selector, Browse, and Coding offer."""
    return [d for d in _all_datasets() if d["status"] != "unloaded"]


# --- Corpus analytics (Analytics tab) -----------------------------------------------
#
# corpus_analytics.py is deliberately pure (only imports coding_store/classifier, never this
# module) so it stays independent of DATASETS/get_dataset() -- this is the one join it can't
# do on its own: a segment's dataset_id/item_id to that document's full metadata record
# (group_name, publish_date, duration_secs, ...), which lives in each dataset's in-memory
# `index`, not coding.db. Every endpoint below builds this once per request and hands it to
# corpus_analytics.py's functions as a plain argument.

def _build_item_meta(segment_rows):
    """{(dataset_id, item_id): index_record} for every dataset_id present in segment_rows.
    get_dataset() already returns None for an 'unloaded' dataset, but that can't happen here
    in practice -- segment_rows comes from corpus_analytics.get_corpus_scope(), which already
    excludes 'excluded'/'unloaded' datasets via classifier.build_corpus()'s own dataset_status
    check; the .get(...) guard below is just defensive, not a case this should ever hit."""
    item_meta = {}
    for dataset_id in {r["dataset_id"] for r in segment_rows}:
        dataset = get_dataset(dataset_id)
        for rec in (dataset or {}).get("index", []):
            item_meta[(dataset_id, str(rec.get("item_id")))] = rec
    return item_meta


# --- Background query/export jobs -------------------------------------------------
#
# JOBS and TRAIN_JOBS below are both jobs.JobRegistry instances -- see that module for
# the shared start/mutate/update/get shape this section and the next one build on, and
# docs/DEVELOPMENT.md's "Background jobs" convention for why a third job type should also
# use it rather than hand-rolling its own {job_id: {...}} dict.

JOBS = JobRegistry()


def run_query_job(job_id, registry, kind, source_id, label, company_name, after_dt, before_dt):
    def on_page(total, has_next):
        registry.mutate(job_id, lambda job: job.update(
            items_fetched=total, pages_fetched=job["pages_fetched"] + 1,
        ))

    fetch_kwargs = dict(after_dt=after_dt or None, before_dt=before_dt or None, on_page=on_page)
    if kind == "company":
        items = query_api.fetch_feed(company_id=source_id, **fetch_kwargs)
    else:
        items = query_api.fetch_feed(entity_id=source_id, **fetch_kwargs)

    rows = [query_api.build_row(it, company_name) for it in items]

    date_bit = ""
    if after_dt or before_dt:
        date_bit = f" [{after_dt or '…'} → {before_dt or '…'}]"
    dataset_label = f"{label}{date_bit} — {len(rows)} interviews"

    result = _create_dataset(
        rows, filename_prefix=kind, registry_kind=kind, label=label, dataset_label=dataset_label,
        source_id=source_id, segment=False, action_type="query_download",
        action_details={"kind": kind, "source_id": source_id, "label": label,
                         "after": after_dt, "before": before_dt},
    )

    registry.update(
        job_id, status="done", dataset_id=result["dataset_id"], dataset_label=result["dataset_label"],
        json_file=result["json_file"], csv_file=result["csv_file"],
    )


# --- Background classifier training jobs (Model tab) --------------------------------

TRAIN_JOBS = JobRegistry()


def _skipped_entry(result):
    """One job-status entry for a theme a training pass skipped, saying what it
    still needs: more positives, or (in the "reviewed" negative mode) more
    judged negatives. needed/have are for the UI's "x/needed" wording."""
    status = result["status"]
    if status == "skipped_insufficient_negatives":
        needed, have = classifier.MIN_NEGATIVES_REVIEWED, result.get("n_neg", 0)
    elif status == "skipped_below_trust_floor":
        needed, have = classifier.TRUST_POSITIVES, result["n_pos"]
    else:
        needed, have = classifier.MIN_POSITIVES_ATTEMPT, result["n_pos"]
    return {"theme_id": result["theme_id"], "name": result["name"], "status": status,
            "n_pos": result["n_pos"], "n_neg": result.get("n_neg"), "needed": needed, "have": have}


def _run_training_job(job_id, registry, pass_fn, log_action, scheme_id, negative_mode):
    """Shared body of the TF-IDF and embedding training jobs: run pass_fn
    (classifier.run_training_pass / run_embedding_training_pass) with live
    progress, then record the final trained/skipped lists and log the run."""
    def on_progress(event):
        stage = event["stage"]
        if stage not in ("theme_start", "theme_skipped", "theme_done"):
            return

        def apply(job):
            job["themes_total"] = event["total"]
            job["current_theme"] = {"theme_id": event["theme_id"], "name": event["name"]}
            if stage == "theme_skipped":
                job["themes_done"] = event["index"] + 1
                job["skipped"].append(_skipped_entry(event))
            elif stage == "theme_done":
                job["themes_done"] = event["index"] + 1
                job["trained"].append({
                    "theme_id": event["theme_id"], "name": event["name"], **event["metrics"],
                })

        registry.mutate(job_id, apply)

    summary = pass_fn(progress_callback=on_progress, scheme_id=scheme_id, negative_mode=negative_mode)
    if summary["error"]:
        registry.update(job_id, status="error", error=summary["error"])
        return

    # authoritative final lists, in case an incremental update above was missed
    trained = [
        {"theme_id": r["theme_id"], "name": r["name"], **r["metrics"]}
        for r in summary["results"] if r["status"] == "trained"
    ]
    skipped = [_skipped_entry(r) for r in summary["results"] if r["status"].startswith("skipped")]
    registry.update(
        job_id, status="done", model_version=summary["model_version"],
        themes_total=summary["themes_total"], themes_done=summary["themes_total"],
        trained=trained, skipped=skipped,
    )
    activity_log.log_activity(CODER_NAME, log_action, {
        "model_version": summary["model_version"], "scheme_id": scheme_id, "negative_mode": negative_mode,
        "trained": [{"theme_id": t["theme_id"], "name": t["name"]} for t in trained],
        "skipped": [{"theme_id": t["theme_id"], "name": t["name"], "status": t["status"]} for t in skipped],
    })


def run_train_job(job_id, registry, scheme_id=None, negative_mode=classifier.DEFAULT_NEGATIVE_MODE):
    _run_training_job(job_id, registry, classifier.run_training_pass, "training_run", scheme_id, negative_mode)


# --- Background embedding-training jobs (Model tab's optional richer engine) ---------

EMBED_TRAIN_JOBS = JobRegistry()


def run_train_embeddings_job(job_id, registry, scheme_id=None, negative_mode=classifier.DEFAULT_NEGATIVE_MODE):
    _run_training_job(job_id, registry, classifier.run_embedding_training_pass, "embedding_training_run",
                      scheme_id, negative_mode)


# --- Storage maintenance jobs (Model tab's Storage panel) ---------------------------
#
# Pruning and compaction both write across the whole predictions table, and VACUUM
# locks the entire database while it runs. maintenance_conflict() is the one place
# that decides what may overlap: neither may start while training is running (or
# while the other is), training may not start while either is running, and
# switch_project() refuses while either is running, since get_conn() would
# otherwise start pointing at a different coding.db halfway through.

PRUNE_JOBS = JobRegistry()
COMPACT_JOBS = JobRegistry()


def maintenance_conflict(starting):
    """An error string if `starting` ("prune" | "compact" | "train") would overlap
    a job it mustn't, else None."""
    if starting in ("prune", "compact") and (TRAIN_JOBS.has_running() or EMBED_TRAIN_JOBS.has_running()):
        return "A training job is running -- wait for it to finish first."
    if PRUNE_JOBS.has_running():
        return "Old scores are being pruned -- wait for that to finish first."
    if COMPACT_JOBS.has_running():
        return "The database is being compacted -- wait for that to finish first."
    return None


def run_prune_job(job_id, registry, pairs, keep_latest):
    def on_progress(event):
        registry.update(job_id, pairs_done=event["index"], pairs_total=event["total"])
    result = storage.prune_predictions(pairs, keep_latest=keep_latest, actor=CODER_NAME,
                                       progress_callback=on_progress)
    registry.update(job_id, status="done", pairs_done=result["pairs"], **result)


def run_compact_job(job_id, registry):
    result = storage.compact_database(actor=CODER_NAME)
    registry.update(job_id, status="done", **result)


# --- Installing the optional embedding-engine dependency (Model tab) ----------------
#
# A researcher can click a button rather than open a terminal -- runs the exact same
# `pip install -r requirements-embeddings.txt` the docs describe, via sys.executable so
# it installs into whichever Python this server is already running under (not whatever
# `pip` happens to resolve to on PATH). Idempotent -- pip no-ops if it's already
# installed, so there's no need to check embedding_readiness() first.

INSTALL_EMBEDDINGS_JOBS = JobRegistry()


def run_install_embeddings_job(job_id, registry):
    requirements_path = HERE / "requirements-embeddings.txt"
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(requirements_path)],
        capture_output=True, text=True, timeout=1800,  # generous ceiling -- first install
                                                         # downloads PyTorch, hundreds of MB
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-4000:]
        registry.update(job_id, status="error", error=tail or "pip install failed.")
        return
    registry.update(job_id, status="done")


# --- Background segmentation jobs (Coding tab's Datasets panel) ---------------------
#
# The in-app equivalent of scripts/build_segments.py -- but that script only reads
# queries/_index.json, so it can never reach "primary"/"demo" (they're loaded straight
# from DATA_FILE/DEMO_FILE at startup, never registered there). This is also why it
# can't reuse get_dataset(dataset_id)["by_id"].values(): build_dataset() already pops
# "turns" off every cached record (not used by the viewer), so segmentation.
# segment_interview() -- which needs turns -- would silently produce zero segments.
# Records have to be read fresh from disk per dataset kind instead, same three sources
# list_datasets()/build_dataset() already know about.

SEGMENT_JOBS = JobRegistry()


def _load_dataset_records_with_turns(dataset_id):
    """Like get_dataset(), but returns the raw records straight off disk -- with
    `turns` intact -- instead of the cached, turns-stripped copies get_dataset()
    hands every other caller. Returns None if the dataset can't be found or read."""
    if dataset_id == "primary":
        path = DATA_FILE
    elif dataset_id == "demo":
        path = DEMO_FILE
    else:
        entry = next((e for e in load_query_registry() if e["id"] == dataset_id), None)
        if entry is None:
            return None
        path = QUERIES_DIR / entry["json_file"]
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
        query_api.validate_dataset_records(records, path.name)
    except (json.JSONDecodeError, query_api.DatasetValidationError) as exc:
        print(f"Dataset '{dataset_id}' failed to load for segmenting: {exc}")
        return None
    return records


def run_segment_job(job_id, registry, dataset_ids):
    """Segments every dataset in dataset_ids into coding.db (segment_new_dataset()'s
    upsert is idempotent, so re-running this on an already-segmented dataset is safe
    and just a no-op refresh). dataset_ids=None means every dataset list_datasets()
    currently reports zero segments for -- the "close the gap" default a plain
    "Segment everything" button uses."""
    if not dataset_ids:
        dataset_ids = [d["id"] for d in list_datasets() if d["segment_count"] == 0]

    registry.update(job_id, datasets_total=len(dataset_ids))

    results = []
    for i, dataset_id in enumerate(dataset_ids):
        registry.mutate(job_id, lambda job: job.update(current_dataset=dataset_id, datasets_done=i))
        records = _load_dataset_records_with_turns(dataset_id)
        if records is None:
            results.append({"dataset_id": dataset_id, "status": "skipped", "reason": "couldn't read dataset"})
            continue
        segments_added = segment_new_dataset(records, dataset_id)
        results.append({"dataset_id": dataset_id, "status": "done", "segments_added": segments_added})

    total_segments = sum(r.get("segments_added", 0) for r in results)
    registry.update(
        job_id, status="done", datasets_done=len(dataset_ids), current_dataset=None,
        results=results, segments_added_total=total_segments,
    )
    activity_log.log_activity(CODER_NAME, "segments_built", {
        "dataset_ids": dataset_ids, "segments_added_total": total_segments,
    })


# --- Background duplicate-scan jobs (Coding tab's Datasets panel) -------------------

DUPLICATE_JOBS = JobRegistry()


def run_duplicate_job(job_id, registry, dataset_ids):
    def on_progress(event):
        stage = event["stage"]
        if stage == "loaded":
            registry.update(job_id, n_items_scanned=event["n_items_scanned"], n_datasets=event["n_datasets"])
        elif stage == "clustering":
            registry.update(job_id, n_candidate_pairs=event["n_candidate_pairs"])
        elif stage == "cluster_done":
            registry.update(job_id, clusters_done=event["index"], clusters_total=event["total"])

    summary = duplicate_scanner.run_duplicate_scan(
        dataset_ids=dataset_ids, progress_callback=on_progress, actor=CODER_NAME,
    )
    registry.update(
        job_id, status="done", run_id=summary["run_id"],
        n_items_scanned=summary["n_items_scanned"], n_clusters=len(summary["clusters"]),
        needs_attention_total=summary["needs_attention_total"],
        coded_flagged_total=summary["coded_flagged_total"],
    )


# --- Import (Search & Export tab's Import panel) -------------------------------------
#
# A two-step flow, unlike query jobs above: /parse reads uploaded file(s) into memory and
# returns a preview -- for a transcript, the speaker labels found, so the researcher can
# assign interviewer/respondent/other roles in the browser (the same decision
# import_interview_transcript.py's CLI makes via interactive prompts); for Reddit/Arctic
# Shift, just item/turn counts (no per-source decision needed there). Nothing is written
# to queries/ until /commit. PENDING_IMPORTS holds the parsed-but-uncommitted upload
# between those two calls -- shared by both import kinds, keyed by a one-time token and
# tagged with "kind" ("transcript"/"json"/"reddit") so /commit knows how to finish it.
# Entries are popped (not just read) on commit, and a stale/abandoned one is harmless --
# this is single-researcher local state, not something worth a TTL sweep for.

PENDING_IMPORTS = {}
PENDING_IMPORTS_LOCK = threading.Lock()

# RAW_UPLOADS holds decoded-text file uploads between /api/import/raw_upload and a
# follow-up parse call (currently just /api/import/reddit/parse). Reddit's Arctic Shift
# comments export routinely reaches hundreds of MB -- base64-encoding that in the browser
# and wrapping it in a JSON body (the transcript importer's approach, fine for one
# interview file) blows past a tab's memory budget: a 349 MB file needs a ~349 MB
# ArrayBuffer, then a ~465 MB base64 string, then another ~465 MB+ copy when
# JSON.stringify serializes the request body -- several of these alive at once. Posting
# the File object directly as the request body instead lets the browser stream it to the
# network without ever materializing it as a JS string, so raw_upload reads it here and
# hands back a small token for the parse call to reference. Entries are popped (not just
# read) on use; a stale/abandoned one (upload started, parse never called) is harmless for
# the same reason PENDING_IMPORTS's are.
RAW_UPLOADS = {}
RAW_UPLOADS_LOCK = threading.Lock()


def segment_new_dataset(records, dataset_id):
    """Segments a freshly-imported dataset's records and stores them in
    coding.db immediately, so a browser-based import lands ready for the
    Coding tab the moment it shows up in Browse -- unlike a Search & Export
    download or a scripts/import_*.py CLI run, which still need
    scripts/build_segments.py run by hand afterward. Mirrors that script's
    per-dataset loop (segmentation.segment_interview + upsert_segments).
    Returns the segment count."""
    all_segments = []
    for record in records:
        segs = segmentation.segment_interview(record)
        for seg in segs:
            seg["dataset_id"] = dataset_id
        all_segments.extend(segs)
    segments_store.upsert_segments(all_segments)
    return len(all_segments)


# --- Turning a batch of records into (or into an existing) browsable dataset --------
#
# Every producer of a new dataset -- a Search & Export download, a browser-based
# transcript import, a browser-based Reddit import -- does the same "write it to
# queries/, register it, maybe segment it, log it" ceremony. These two helpers are
# that ceremony, once. Callers below differ only in what they pass in: the
# registry kind, the filename prefix (not always the same as the registry kind --
# see each call site), whether to segment immediately, and which activity_log
# action_type/details apply.

def _create_dataset(records, *, filename_prefix, registry_kind, label, dataset_label,
                     source_id=None, segment=False, action_type, action_details=None):
    """Writes records as a brand-new dataset (queries/<prefix>_<slug>_<stamp>.{json,csv},
    registered under a fresh dataset_id) and logs one activity_log entry. segment=True
    also segments into coding.db immediately (see segment_new_dataset) -- used by the
    two import paths, not by Search & Export, which still relies on a separate
    scripts/build_segments.py run. Returns {dataset_id, dataset_label, json_file,
    csv_file, count, segments_added} -- segments_added is None when segment=False."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base_name = f"{filename_prefix}_{query_api.slugify(label)}_{stamp}"
    json_path, csv_path = query_api.write_export(QUERIES_DIR, base_name, records)
    dataset_id = f"q_{uuid.uuid4().hex[:10]}"
    query_api.register_export(
        QUERIES_DIR,
        dataset_id=dataset_id,
        label=dataset_label,
        kind=registry_kind,
        source_id=source_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        count=len(records),
        json_path=json_path,
        csv_path=csv_path,
    )
    segments_added = segment_new_dataset(records, dataset_id) if segment else None

    details = {**(action_details or {}), "dataset_id": dataset_id, "count": len(records)}
    if segment:
        details["segments_added"] = segments_added
    activity_log.log_activity(CODER_NAME, action_type, details)

    return {
        "dataset_id": dataset_id, "dataset_label": dataset_label,
        "json_file": json_path.name, "csv_file": csv_path.name,
        "count": len(records), "segments_added": segments_added,
    }


def _append_dataset(records, *, target_dataset_id, expected_kind, wrong_kind_message,
                     count_noun="interview(s)", action_type, action_details=None):
    """Adds records into an existing dataset (query_api.append_to_dataset) instead of
    creating a new one, then segments only the new records and logs one activity_log
    entry tagged appended=True. Raises ApiError (400) if target_dataset_id is unknown
    or isn't a dataset of expected_kind -- appending into a live-downloaded dataset
    would conflate a hand-uploaded/imported batch with a query result. Returns the same
    shape as _create_dataset(), plus "appended": True."""
    entry = next((e for e in query_api.load_registry(QUERIES_DIR) if e["id"] == target_dataset_id), None)
    if entry is None:
        raise ApiError("unknown target dataset -- it may have been removed", status=400)
    if entry.get("kind") != expected_kind:
        raise ApiError(wrong_kind_message, status=400)
    try:
        _, updated_entry = query_api.append_to_dataset(
            QUERIES_DIR, target_dataset_id, records, count_noun=count_noun
        )
    except query_api.DatasetValidationError as exc:
        raise ApiError(str(exc), status=400)

    segments_added = segment_new_dataset(records, target_dataset_id)
    with DATASETS_LOCK:
        DATASETS.pop(target_dataset_id, None)  # force a reload from disk next access

    details = {
        **(action_details or {}), "dataset_id": target_dataset_id, "label": updated_entry["label"],
        "count": len(records), "segments_added": segments_added, "appended": True,
    }
    activity_log.log_activity(CODER_NAME, action_type, details)

    return {
        "dataset_id": target_dataset_id, "dataset_label": updated_entry["label"],
        "json_file": updated_entry["json_file"], "csv_file": updated_entry["csv_file"],
        "count": len(records), "segments_added": segments_added, "appended": True,
    }


# --- In-app project switching -------------------------------------------------------

PROJECT_SWITCH_LOCK = threading.Lock()


def switch_project(new_dir):
    """Live-switches the running server's active project (queries/coding.db/
    exports/) to new_dir, without a restart. Returns None on success, or an
    error string. See project_registry.py for the recent-projects list this
    backs, and paths.py for why data/demo_dataset.json/executive_interviews.json
    are untouched here -- they're repo-level, not project-specific."""
    global QUERIES_DIR, EXPORTS_DIR, CURRENT_PROJECT_DIR
    new_dir = Path(new_dir)

    if JOBS.has_running():
        return "A download job is still running -- wait for it to finish before switching projects."
    if TRAIN_JOBS.has_running():
        return "A training job is still running -- wait for it to finish before switching projects."
    if PRUNE_JOBS.has_running() or COMPACT_JOBS.has_running():
        return "Storage maintenance is still running -- wait for it to finish before switching projects."

    with PROJECT_SWITCH_LOCK:
        try:
            new_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"Couldn't create/access {new_dir}: {exc}"

        QUERIES_DIR = new_dir / "queries"
        EXPORTS_DIR = new_dir / "exports"
        schema.set_db_path(new_dir / "coding.db")
        # coding_store.embeddings reads paths.EMBEDDINGS_DIR directly (not a copy), so it's
        # this attribute -- on the actual paths module object -- that needs reassigning, the
        # same way schema.set_db_path() reassigns schema.DB_PATH.
        project_paths.EMBEDDINGS_DIR = new_dir / "embeddings"
        CURRENT_PROJECT_DIR = new_dir

        with DATASETS_LOCK:
            for key in [k for k in DATASETS if k not in ("primary", "demo")]:
                del DATASETS[key]

        project_registry.register_project(new_dir)
    return None


def browse_for_folder():
    """Pops a native OS folder-picker dialog server-side (stdlib tkinter, no
    new dependency) and returns the chosen path, or None if cancelled/
    unavailable. This app runs locally on the researcher's own machine, so a
    GUI dialog popped from a request-handling thread is safe in practice;
    callers must treat any exception as "fall back to the text field," not a
    hard failure -- headless environments won't have a display for this."""
    with PROJECT_SWITCH_LOCK:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            path = filedialog.askdirectory()
        finally:
            root.destroy()
        return path or None


ROUTER = Router()

# --- Static files (viewer.html + the plain-<script>-tag JS/CSS it loads) -----------

STATIC_FILES = {
    "/": "viewer.html",
    "/index.html": "viewer.html",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
    "/coding.js": "coding.js",
    "/analytics.js": "analytics.js",
    "/model.js": "model.js",
    "/storage.js": "storage.js",
    "/codebook.js": "codebook.js",
    "/runs.js": "runs.js",
    "/review.js": "review.js",
    "/cycle.js": "cycle.js",
    "/appendix.js": "appendix.js",
    "/projects.js": "projects.js",
    "/import.js": "import.js",
}


def _static_route(filename):
    def handler(req):
        return FileResponse(STATIC_DIR / filename)
    return handler


for _url_path, _filename in STATIC_FILES.items():
    ROUTER.get(_url_path)(_static_route(_filename))


# --- Small domain helpers shared by more than one route below ----------------------

def _require_dataset(dataset_id):
    dataset = get_dataset(dataset_id)
    if dataset is None:
        raise ApiError(f"unknown dataset '{dataset_id}'", status=404)
    return dataset


# --- Browse ---------------------------------------------------------------------

@ROUTER.get("/api/datasets")
def get_datasets(req):
    return JsonResponse(list_datasets())


@ROUTER.get("/api/datasets/all")
def get_datasets_all(req):
    return JsonResponse(_all_datasets())


@ROUTER.get("/api/index")
def get_index(req):
    dataset = _require_dataset(req.query.get("dataset", "primary"))
    return JsonResponse(dataset["index"])


@ROUTER.get("/api/interview/<id>")
def get_interview(req):
    dataset = _require_dataset(req.query.get("dataset", "primary"))
    try:
        iid = int(req.path_params["id"])
    except ValueError:
        raise ApiError("interview id must be an integer", status=400)
    rec = dataset["by_id"].get(iid)
    if rec is None:
        raise ApiError(f"unknown interview id {iid}", status=404)
    return JsonResponse(rec)


@ROUTER.get("/api/transcript_search")
def get_transcript_search(req):
    dataset = _require_dataset(req.query.get("dataset", "primary"))
    term = (req.query.get("q", "")).strip().lower()
    if not term:
        return JsonResponse({"ids": []})
    ids = [
        rid for rid, rec in dataset["by_id"].items()
        if term in (rec.get("transcript_text") or "").lower()
    ]
    return JsonResponse({"ids": ids})


# --- Search & Export --------------------------------------------------------------

@ROUTER.get("/api/search/companies")
def get_search_companies(req):
    keyword = (req.query.get("keyword", "")).strip()
    if not keyword:
        return JsonResponse({"results": []})
    try:
        results = query_api.search_companies(keyword)
    except query_api.ApiCredentialsError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    return JsonResponse({"results": results})


@ROUTER.get("/api/search/entities")
def get_search_entities(req):
    keyword = (req.query.get("keyword", "")).strip()
    if not keyword:
        return JsonResponse({"results": []})
    try:
        results = query_api.search_entities(keyword)
    except query_api.ApiCredentialsError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    return JsonResponse({"results": results})


@ROUTER.post("/api/query/start", json_body=True)
def post_query_start(req):
    body = req.body
    kind = body.get("kind")
    source_id = body.get("id")
    label = (body.get("label") or "").strip()
    company_name = (body.get("company_name") or label).strip()
    after_dt = (body.get("after") or "").strip() or None
    before_dt = (body.get("before") or "").strip() or None

    if kind not in ("company", "entity") or not source_id or not label:
        return JsonResponse({"error": "kind ('company'|'entity'), id, and label are required"}, status=400)

    job_id = JOBS.start(
        {"kind": kind, "label": label, "items_fetched": 0, "pages_fetched": 0},
        run_query_job, kind, source_id, label, company_name, after_dt, before_dt,
    )
    return JsonResponse({"job_id": job_id})


@ROUTER.get("/api/query/status")
def get_query_status(req):
    job = JOBS.get(req.query.get("job_id", ""))
    if job is None:
        return JsonResponse({"error": "unknown job_id"}, status=404)
    return JsonResponse(job)


# --- Coding: schemes (independent codebooks) ------------------------------------------

@ROUTER.get("/api/schemes")
def get_schemes(req):
    include_archived = req.query.get("include_archived", "") == "1"
    return JsonResponse(schemes.list_schemes(include_archived=include_archived))


@ROUTER.post("/api/schemes", json_body=True, required=["name"])
def post_schemes(req):
    body = req.body
    try:
        scheme = schemes.create_scheme(
            name=body["name"], description=body.get("description", ""),
            actor=body.get("coder") or CODER_NAME,
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(scheme, status=201)


@ROUTER.post("/api/schemes/<scheme_id>/update", json_body=True, loaders={"scheme_id": schemes.get_scheme})
def post_scheme_update(req):
    scheme_id = req.path_params["scheme_id"]
    return JsonResponse(schemes.update_scheme(scheme_id, req.body, actor=req.body.get("coder") or CODER_NAME))


@ROUTER.post("/api/schemes/<scheme_id>/archive", json_body="optional", loaders={"scheme_id": schemes.get_scheme})
def post_scheme_archive(req):
    scheme_id = req.path_params["scheme_id"]
    return JsonResponse(schemes.set_scheme_status(scheme_id, "archived", actor=req.body.get("coder") or CODER_NAME))


@ROUTER.post("/api/schemes/<scheme_id>/restore", json_body="optional", loaders={"scheme_id": schemes.get_scheme})
def post_scheme_restore(req):
    scheme_id = req.path_params["scheme_id"]
    return JsonResponse(schemes.set_scheme_status(scheme_id, "active", actor=req.body.get("coder") or CODER_NAME))


# --- Coding: codebook ---------------------------------------------------------------

@ROUTER.get("/api/codebook/themes")
def get_codebook_themes(req):
    include_archived = req.query.get("include_archived", "") == "1"
    return JsonResponse(themes.list_themes(
        include_archived=include_archived, scheme_id=req.query.get("scheme_id"),
    ))


@ROUTER.post("/api/codebook/themes", json_body=True, required=["name", "scheme_id"])
def post_codebook_themes(req):
    body = req.body
    try:
        theme = themes.create_theme(
            name=body.get("name", ""),
            description=body.get("description", ""),
            example_words=body.get("example_words", ""),
            color=body.get("color") or "#6b7fd7",
            scheme_id=body["scheme_id"],
            parent_theme_id=body.get("parent_theme_id") or None,
            actor=body.get("coder") or CODER_NAME,
            inclusion_criteria=body.get("inclusion_criteria"),
            exclusion_criteria=body.get("exclusion_criteria"),
            boundary_notes=body.get("boundary_notes"),
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(theme, status=201)


@ROUTER.post("/api/codebook/flag", json_body=True, required=["segment_id", "theme_id", "kind"])
def post_codebook_flag(req):
    body = req.body
    try:
        result = examples_store.set_flag(
            body["segment_id"], body["theme_id"], body["kind"], bool(body.get("flagged", True)),
            note=body.get("note"), coder=body.get("coder") or CODER_NAME,
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"ok": True, **result})


@ROUTER.get("/api/codebook/examples")
def get_codebook_examples(req):
    theme_id = req.query.get("theme_id") or ""
    if themes.get_theme(theme_id) is None:
        return JsonResponse({"error": f"unknown theme '{theme_id}'"}, status=400)
    return JsonResponse({
        "exemplars": examples_store.annotate_twins(examples_store.list_flagged(theme_id, "exemplar")),
        "near_misses": examples_store.annotate_twins(examples_store.list_flagged(theme_id, "near_miss")),
    })


@ROUTER.get("/api/codebook/overview")
def get_codebook_overview(req):
    scheme_id = req.query.get("scheme_id") or ""
    if schemes.get_scheme(scheme_id) is None:
        return JsonResponse({"error": f"unknown scheme '{scheme_id}'"}, status=400)
    return JsonResponse(codebook.overview(scheme_id))


@ROUTER.post("/api/codebook/themes/<theme_id>/update", json_body=True, loaders={"theme_id": themes.get_theme})
def post_theme_update(req):
    theme_id = req.path_params["theme_id"]
    try:
        updated = themes.update_theme(theme_id, req.body, actor=req.body.get("coder") or CODER_NAME)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(updated)


@ROUTER.post("/api/codebook/themes/<theme_id>/archive", json_body="optional", loaders={"theme_id": themes.get_theme})
def post_theme_archive(req):
    theme_id = req.path_params["theme_id"]
    return JsonResponse(themes.set_theme_status(theme_id, "archived", actor=req.body.get("coder") or CODER_NAME))


@ROUTER.post("/api/codebook/themes/<theme_id>/restore", json_body="optional", loaders={"theme_id": themes.get_theme})
def post_theme_restore(req):
    theme_id = req.path_params["theme_id"]
    return JsonResponse(themes.set_theme_status(theme_id, "active", actor=req.body.get("coder") or CODER_NAME))


@ROUTER.post("/api/codebook/themes/<theme_id>/merge_into", json_body=True, loaders={"theme_id": themes.get_theme})
def post_theme_merge_into(req):
    theme_id = req.path_params["theme_id"]
    body = req.body
    target_theme_id = body.get("target_theme_id")
    target = themes.get_theme(target_theme_id) if target_theme_id else None
    if target is None:
        return JsonResponse({"error": "target_theme_id must reference an existing theme"}, status=400)
    if target["status"] != "active":
        return JsonResponse({"error": "cannot merge into an archived theme"}, status=400)
    try:
        merged = themes.merge_themes(theme_id, target_theme_id, actor=body.get("coder") or CODER_NAME)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(merged)


# --- Coding: segments/codes ----------------------------------------------------------

@ROUTER.get("/api/coding/segments")
def get_coding_segments(req):
    item_id = (req.query.get("item_id", "")).strip()
    if not item_id:
        return JsonResponse({"error": "item_id is required"}, status=400)
    scheme_id = req.query.get("scheme_id") or None
    segments = segments_store.get_segments_for_item(item_id)
    codes_by_segment = codes_store.get_codes_for_segment_ids([s["segment_id"] for s in segments])
    rejections_by_segment = codes_store.get_negatives_for_segment_ids([s["segment_id"] for s in segments])
    exemplars_by_segment = examples_store.flagged_theme_ids([s["segment_id"] for s in segments], "exemplar",
                                                             coder=CODER_NAME)
    for s in segments:
        s["theme_ids"] = codes_by_segment.get(s["segment_id"], [])
        s["rejected_theme_ids"] = rejections_by_segment.get(s["segment_id"], [])
        s["exemplar_theme_ids"] = exemplars_by_segment.get(s["segment_id"], [])
    # "Reviewed" state for the requested scheme (roadmap A6) -- see coding_store/reviewed.py.
    item_review = None
    if scheme_id:
        confirmed = reviewed_store.get_none_confirmed_segment_ids([s["segment_id"] for s in segments], scheme_id)
        for s in segments:
            s["none_confirmed"] = s["segment_id"] in confirmed
        if segments:
            item_review = reviewed_store.get_item_review(segments[0]["dataset_id"], item_id, scheme_id)
            if item_review:
                # How far the mark can be trusted as "read in full" (CODEBOOK_PLAN K5).
                coded = reviewed_store.coded_segment_counts(scheme_id).get(
                    (segments[0]["dataset_id"], str(item_id)), 0)
                item_review = {**item_review, "status": reviewed_store.mark_status(item_review),
                               "coded_segments": coded,
                               "inferred_by_coverage": coded >= reviewed_store.READ_IN_FULL_MIN_CODED}
    return JsonResponse({"item_id": item_id, "segments": segments, "item_review": item_review})


@ROUTER.post("/api/coding/item_review", json_body=True, required=["dataset_id", "item_id", "scheme_id"])
def post_coding_item_review(req):
    body = req.body
    if schemes.get_scheme(body["scheme_id"]) is None:
        return JsonResponse({"error": f"unknown scheme '{body['scheme_id']}'"}, status=400)
    review_row = reviewed_store.set_item_review(
        body["dataset_id"], body["item_id"], body["scheme_id"], bool(body.get("reviewed", True)),
        coder=body.get("coder") or CODER_NAME,
    )
    if review_row:
        coded = reviewed_store.coded_segment_counts(body["scheme_id"]).get(
            (body["dataset_id"], str(body["item_id"])), 0)
        review_row = {**review_row, "status": reviewed_store.mark_status(review_row), "coded_segments": coded,
                      "inferred_by_coverage": coded >= reviewed_store.READ_IN_FULL_MIN_CODED}
    return JsonResponse({"ok": True, "item_review": review_row})


@ROUTER.post("/api/coding/item_review/confirm", json_body=True, required=["dataset_id", "item_id", "scheme_id"])
def post_coding_item_review_confirm(req):
    body = req.body
    try:
        mark = reviewed_store.set_read_in_full_confirmed(
            body["dataset_id"], body["item_id"], body["scheme_id"], bool(body.get("confirmed", True)),
            coder=body.get("coder") or CODER_NAME,
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "item_review": {**mark, "status": reviewed_store.mark_status(mark)}})


@ROUTER.get("/api/coding/unconfirmed_marks")
def get_coding_unconfirmed_marks(req):
    scheme_id = req.query.get("scheme_id") or ""
    if schemes.get_scheme(scheme_id) is None:
        return JsonResponse({"error": f"unknown scheme '{scheme_id}'"}, status=400)
    marks = reviewed_store.unconfirmed_marks(scheme_id)
    _add_item_labels(marks)
    return JsonResponse(marks)


def _add_item_labels(rows):
    """Adds dataset_label / person_name / item_title to rows with dataset_id + item_id,
    from each dataset's in-memory index (coding.db holds no titles)."""
    index_by_dataset = {}
    for c in rows:
        if c["dataset_id"] not in index_by_dataset:
            dataset = get_dataset(c["dataset_id"])
            index_by_dataset[c["dataset_id"]] = (
                (dataset or {}).get("label", c["dataset_id"]),
                {str(r.get("item_id")): r for r in (dataset or {}).get("index", [])},
            )
        label, records = index_by_dataset[c["dataset_id"]]
        rec = records.get(str(c["item_id"]), {})
        c.update(dataset_label=label, person_name=rec.get("person_name"), item_title=rec.get("item_title"))
    return rows


@ROUTER.get("/api/coding/backfill_candidates")
def get_coding_backfill_candidates(req):
    scheme_id = req.query.get("scheme_id") or ""
    if schemes.get_scheme(scheme_id) is None:
        return JsonResponse({"error": f"unknown scheme '{scheme_id}'"}, status=400)
    return JsonResponse(_add_item_labels(reviewed_store.backfill_candidates(scheme_id)))


@ROUTER.post("/api/coding/item_reviews/bulk", json_body=True, required=["scheme_id", "items", "as_of"])
def post_coding_item_reviews_bulk(req):
    body = req.body
    if schemes.get_scheme(body["scheme_id"]) is None:
        return JsonResponse({"error": f"unknown scheme '{body['scheme_id']}'"}, status=400)
    as_of = body["as_of"]
    if as_of != "last_code":
        # An explicit "as of" moment: normalized to UTC in the same isoformat as every
        # other timestamp in coding.db, since labels.py compares them as strings.
        try:
            moment = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        except ValueError:
            return JsonResponse({"error": "as_of must be 'last_code' or an ISO date-time"}, status=400)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
        if moment > datetime.now(timezone.utc):
            return JsonResponse({"error": "as_of can't be in the future"}, status=400)
        as_of = moment.isoformat()
    try:
        items = [(i["dataset_id"], i["item_id"]) for i in body["items"]]
    except (TypeError, KeyError):
        return JsonResponse({"error": "items must be a list of {dataset_id, item_id}"}, status=400)
    if not items:
        return JsonResponse({"error": "no interviews selected"}, status=400)
    result = reviewed_store.bulk_mark_items(items, body["scheme_id"], as_of, coder=body.get("coder") or CODER_NAME)
    return JsonResponse({"ok": True, **result})


@ROUTER.post("/api/coding/segment_none", json_body=True, required=["segment_id", "scheme_id"])
def post_coding_segment_none(req):
    body = req.body
    if schemes.get_scheme(body["scheme_id"]) is None:
        return JsonResponse({"error": f"unknown scheme '{body['scheme_id']}'"}, status=400)
    reviewed_store.set_segment_none(
        body["segment_id"], body["scheme_id"], bool(body.get("confirmed", True)),
        coder=body.get("coder") or CODER_NAME,
    )
    return JsonResponse({"ok": True})


@ROUTER.post("/api/coding/codes", json_body=True, required=["segment_id", "theme_id"])
def post_coding_codes(req):
    body = req.body
    codes_store.add_code(
        body["segment_id"], body["theme_id"], coder=body.get("coder") or CODER_NAME,
        source=body.get("source") or "manual", note=body.get("note"),
    )
    return JsonResponse({"ok": True})


@ROUTER.post("/api/coding/codes/delete", json_body=True, required=["segment_id", "theme_id"])
def post_coding_codes_delete(req):
    body = req.body
    codes_store.remove_code(body["segment_id"], body["theme_id"], coder=body.get("coder") or CODER_NAME)
    return JsonResponse({"ok": True})


@ROUTER.post("/api/coding/negatives", json_body=True, required=["segment_id", "theme_id"])
def post_coding_negatives(req):
    body = req.body
    codes_store.add_negative(
        body["segment_id"], body["theme_id"], coder=body.get("coder") or CODER_NAME,
        source=body.get("source") or "manual", note=body.get("note"),
    )
    return JsonResponse({"ok": True})


@ROUTER.post("/api/coding/negatives/delete", json_body=True, required=["segment_id", "theme_id"])
def post_coding_negatives_delete(req):
    body = req.body
    codes_store.remove_negative(body["segment_id"], body["theme_id"], coder=body.get("coder") or CODER_NAME)
    return JsonResponse({"ok": True})


@ROUTER.post("/api/coding/segment_datasets", json_body="optional")
def post_coding_segment_datasets(req):
    dataset_ids = req.body.get("dataset_ids") or None
    job_id = SEGMENT_JOBS.start(
        {"datasets_total": 0, "datasets_done": 0, "current_dataset": None,
         "results": [], "segments_added_total": 0},
        run_segment_job, dataset_ids,
    )
    return JsonResponse({"job_id": job_id})


@ROUTER.get("/api/coding/segment_status")
def get_coding_segment_status(req):
    job = SEGMENT_JOBS.get(req.query.get("job_id", ""))
    if job is None:
        return JsonResponse({"error": "unknown job_id"}, status=404)
    return JsonResponse(job)


@ROUTER.get("/api/coding/progress")
def get_coding_progress(req):
    return JsonResponse(review.get_progress(
        dataset_id=req.query.get("dataset"), scheme_id=req.query.get("scheme_id"),
    ))


@ROUTER.get("/api/coding/review_candidates")
def get_review_candidates(req):
    theme_ids = [t for t in (req.query.get("theme_ids", "")).split(",") if t]
    if not theme_ids:
        return JsonResponse({"error": "theme_ids (comma-separated) is required"}, status=400)
    try:
        predicted_limit = int(req.query.get("predicted_limit", "30"))
    except ValueError:
        predicted_limit = 30
    engine = req.query.get("engine") or "tfidf"
    if engine not in model_runs.ENGINE_PREFIXES:
        return JsonResponse({"error": f"unknown engine '{engine}'"}, status=400)
    return JsonResponse(review.get_review_candidates(theme_ids, predicted_limit=predicted_limit, engine=engine))


@ROUTER.get("/api/review/queue")
def get_review_queue(req):
    theme_id = req.query.get("theme_id") or ""
    mode = req.query.get("mode") or "top"
    engine = req.query.get("engine") or "tfidf"
    if engine not in model_runs.ENGINE_PREFIXES:
        return JsonResponse({"error": f"unknown engine '{engine}'"}, status=400)
    try:
        offset = max(0, int(req.query.get("offset", "0")))
        limit = min(200, max(1, int(req.query.get("limit", str(queues.DEFAULT_PAGE_SIZE)))))
    except ValueError:
        return JsonResponse({"error": "offset and limit must be integers"}, status=400)
    try:
        return JsonResponse(queues.get_queue(theme_id, mode, engine=engine, offset=offset, limit=limit))
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)


MAX_SCORE_SEGMENTS = 300  # a page of Review cards, not a corpus -- keeps the GET URL and query small


@ROUTER.get("/api/review/segment_scores")
def get_review_segment_scores(req):
    segment_ids = [s for s in (req.query.get("segment_ids") or "").split(",") if s][:MAX_SCORE_SEGMENTS]
    scheme_id = req.query.get("scheme_id") or None
    engine = req.query.get("engine") or "tfidf"
    if engine not in model_runs.ENGINE_PREFIXES:
        return JsonResponse({"error": f"unknown engine '{engine}'"}, status=400)
    theme_ids = [t["theme_id"] for t in themes.list_themes(scheme_id=scheme_id)]
    return JsonResponse(model_runs.get_scores_for_segments(segment_ids, theme_ids, engine=engine))


@ROUTER.get("/api/model/top_terms")
def get_model_top_terms(req):
    theme_id = req.query.get("theme_id") or ""
    model_version = req.query.get("model_version") or ""
    return JsonResponse(model_runs.get_top_terms(model_version, theme_id))


@ROUTER.post("/api/review/decisions", json_body=True,
             required=["segment_id", "theme_id", "decision", "mode", "queue_theme_id"])
def post_review_decision(req):
    body = req.body
    if body["mode"] not in queues.QUEUE_MODES:
        return JsonResponse({"error": f"unknown queue mode '{body['mode']}'"}, status=400)
    if themes.get_theme(body["theme_id"]) is None:
        return JsonResponse({"error": f"unknown theme '{body['theme_id']}'"}, status=400)
    try:
        row = decisions_store.record_decision(
            body["segment_id"], body["theme_id"], body["decision"], mode=body["mode"],
            engine=body.get("engine"), model_version=body.get("model_version"),
            queue_theme_id=body["queue_theme_id"], score=body.get("score"), oof_score=body.get("oof_score"),
            coder=body.get("coder") or CODER_NAME, flag=body.get("flag") or None,
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    summary = decisions_store.round_summary(body.get("model_version"), body["queue_theme_id"], body["mode"])
    twins = examples_store.find_twins(body["segment_id"], body["theme_id"], row["flag"]) if row["flag"] else []
    return JsonResponse({"decision": row, "round": summary, "twins": twins})


@ROUTER.post("/api/review/decisions/undo", json_body=True, required=["decision_id"])
def post_review_decision_undo(req):
    try:
        row = decisions_store.undo(int(req.body["decision_id"]), coder=req.body.get("coder") or CODER_NAME)
    except (ValueError, TypeError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    summary = decisions_store.round_summary(row["model_version"], row["queue_theme_id"], row["mode"])
    return JsonResponse({"decision": row, "round": summary})


@ROUTER.get("/api/review/round_summary")
def get_review_round_summary(req):
    return JsonResponse(decisions_store.round_summary(
        req.query.get("model_version"), req.query.get("theme_id"), req.query.get("mode"),
    ))


@ROUTER.get("/api/coding/model_runs")
def get_coding_model_runs(req):
    theme_id = req.query.get("theme_id")
    if theme_id:
        return JsonResponse({"theme_id": theme_id, "history": model_runs.list_model_runs(theme_id)})
    engine = req.query.get("engine") or None
    if engine is not None and engine not in model_runs.ENGINE_PREFIXES:
        return JsonResponse({"error": f"unknown engine '{engine}'"}, status=400)
    return JsonResponse(model_runs.get_latest_model_runs(engine=engine))


# --- Model ---------------------------------------------------------------------------

def _training_scheme_param(body):
    """scheme_id from a training request body: None (train every scheme) when
    omitted/empty, else it must name an existing scheme -- a typo'd id would
    otherwise "train" zero themes and look like a success."""
    scheme_id = (body.get("scheme_id") or "").strip() or None
    if scheme_id and schemes.get_scheme(scheme_id) is None:
        raise ApiError(f"unknown scheme '{scheme_id}'", status=400)
    return scheme_id


def _negative_mode_param(body):
    mode = (body.get("negative_mode") or classifier.DEFAULT_NEGATIVE_MODE).strip()
    if mode not in classifier.NEGATIVE_MODES:
        raise ApiError(f"negative_mode must be one of {list(classifier.NEGATIVE_MODES)}", status=400)
    return mode


@ROUTER.post("/api/model/train", json_body="optional")
def post_model_train(req):
    scheme_id = _training_scheme_param(req.body)
    negative_mode = _negative_mode_param(req.body)
    conflict = maintenance_conflict("train")
    if conflict:
        return JsonResponse({"error": conflict}, status=400)
    job_id = TRAIN_JOBS.start(
        {"themes_total": 0, "themes_done": 0, "current_theme": None,
         "trained": [], "skipped": [], "model_version": None, "scheme_id": scheme_id,
         "negative_mode": negative_mode},
        run_train_job, scheme_id, negative_mode,
    )
    return JsonResponse({"job_id": job_id})


@ROUTER.get("/api/model/train_status")
def get_model_train_status(req):
    job = TRAIN_JOBS.get(req.query.get("job_id", ""))
    if job is None:
        return JsonResponse({"error": "unknown job_id"}, status=404)
    return JsonResponse(job)


@ROUTER.post("/api/model/install_embeddings_deps")
def post_model_install_embeddings_deps(req):
    if INSTALL_EMBEDDINGS_JOBS.has_running():
        return JsonResponse({"error": "An install is already in progress."}, status=400)
    job_id = INSTALL_EMBEDDINGS_JOBS.start({}, run_install_embeddings_job)
    return JsonResponse({"job_id": job_id})


@ROUTER.get("/api/model/install_embeddings_deps_status")
def get_model_install_embeddings_deps_status(req):
    job = INSTALL_EMBEDDINGS_JOBS.get(req.query.get("job_id", ""))
    if job is None:
        return JsonResponse({"error": "unknown job_id"}, status=404)
    return JsonResponse(job)


@ROUTER.get("/api/model/embedding_readiness")
def get_model_embedding_readiness(req):
    return JsonResponse(classifier.embedding_readiness(scheme_id=req.query.get("scheme_id")))


@ROUTER.post("/api/model/train_embeddings", json_body="optional")
def post_model_train_embeddings(req):
    scheme_id = _training_scheme_param(req.body)
    negative_mode = _negative_mode_param(req.body)
    readiness = classifier.embedding_readiness(scheme_id=scheme_id)
    if not readiness["installed"]:
        return JsonResponse({
            "error": "sentence-transformers isn't installed -- run "
                     "`pip install -r requirements-embeddings.txt` first.",
        }, status=400)
    if not readiness["volume_ready"]:
        return JsonResponse({
            "error": f"No theme has reached the {readiness['trust_positives']}-positive "
                     "trust floor yet -- keep coding, then try again.",
        }, status=400)
    conflict = maintenance_conflict("train")
    if conflict:
        return JsonResponse({"error": conflict}, status=400)
    job_id = EMBED_TRAIN_JOBS.start(
        {"themes_total": 0, "themes_done": 0, "current_theme": None,
         "trained": [], "skipped": [], "model_version": None, "scheme_id": scheme_id,
         "negative_mode": negative_mode},
        run_train_embeddings_job, scheme_id, negative_mode,
    )
    return JsonResponse({"job_id": job_id})


@ROUTER.get("/api/model/train_embeddings_status")
def get_model_train_embeddings_status(req):
    job = EMBED_TRAIN_JOBS.get(req.query.get("job_id", ""))
    if job is None:
        return JsonResponse({"error": "unknown job_id"}, status=404)
    return JsonResponse(job)


@ROUTER.get("/api/model/interpret")
def get_model_interpret(req):
    theme_id = req.query.get("theme_id", "")
    theme = themes.get_theme(theme_id)
    if theme is None:
        return JsonResponse({"error": f"unknown theme '{theme_id}'"}, status=404)

    runs = model_runs.list_model_runs(theme_id)
    if not runs:
        progress = review.get_progress()
        by_theme = progress["by_theme"].get(theme_id, {"name": theme["name"], "count": 0})
        return JsonResponse({
            "theme_id": theme_id, "trained": False,
            "progress": by_theme, "min_positives": classifier.MIN_POSITIVES_ATTEMPT,
        })

    requested_version = req.query.get("model_version")
    run = next((r for r in runs if r["model_version"] == requested_version), runs[0]) \
        if requested_version else runs[0]
    try:
        limit = int(req.query.get("limit", "15"))
    except ValueError:
        limit = 15

    total_segments = len(classifier.build_corpus()[0])
    return JsonResponse({
        "theme_id": theme_id, "trained": True, "model_version": run["model_version"],
        "metrics": run, "trust_positives_floor": classifier.TRUST_POSITIVES,
        "value_prop": model_runs.get_value_prop(theme_id, run["model_version"], run["threshold"]),
        "coverage": model_runs.get_corpus_coverage(
            theme_id, run["model_version"], run["threshold"], total_segments=total_segments,
        ),
        "top_terms": model_runs.get_top_terms(run["model_version"], theme_id),
        "exemplars": model_runs.get_top_predictions(theme_id, run["model_version"], limit=limit),
    })


@ROUTER.get("/api/engines")
def get_engines(req):
    return JsonResponse({"score_engines": model_runs.engine_registry(), "topic_methods": []})


@ROUTER.get("/api/runs")
def get_runs(req):
    scheme_id = req.query.get("scheme_id") or ""
    if schemes.get_scheme(scheme_id) is None:
        return JsonResponse({"error": f"unknown scheme '{scheme_id}'"}, status=400)
    return JsonResponse({
        "scheme_id": scheme_id,
        "current_codebook_version": codebook_export.current_codebook_version(scheme_id),
        "runs": runs_store.list_runs(scheme_id),
    })


@ROUTER.get("/api/iteration/status")
def get_iteration_status(req):
    scheme_id = req.query.get("scheme_id") or ""
    if schemes.get_scheme(scheme_id) is None:
        return JsonResponse({"error": f"unknown scheme '{scheme_id}'"}, status=400)
    return JsonResponse(iteration.get_status(scheme_id, codebook_export.current_codebook_version(scheme_id)))


@ROUTER.get("/api/model/export_full_corpus")
def get_model_export_full_corpus(req):
    scheme_id = req.query.get("scheme_id")
    model_version = req.query.get("model_version")
    engine = req.query.get("engine") or "tfidf"
    paths = topic_model_export.export_full_corpus_topic_model(
        EXPORTS_DIR, scheme_id=scheme_id, model_version=model_version, engine=engine, actor=CODER_NAME,
    )
    return JsonResponse({"exported_to": str(EXPORTS_DIR), "files": [p.name for p in paths]})


# --- Storage (Model tab's Storage panel) -------------------------------------------

def _keep_latest_param(value):
    try:
        keep_latest = int(value if value is not None else storage.DEFAULT_KEEP_LATEST)
    except (TypeError, ValueError):
        raise ApiError("keep_latest must be an integer", status=400)
    if keep_latest < 1:
        raise ApiError("keep_latest must be at least 1", status=400)
    return keep_latest


@ROUTER.get("/api/storage/report")
def get_storage_report(req):
    return JsonResponse(storage.storage_report(keep_latest=_keep_latest_param(req.query.get("keep_latest"))))


@ROUTER.post("/api/storage/pin", json_body=True, required=["model_version"])
def post_storage_pin(req):
    body = req.body
    try:
        storage.set_pin(body["model_version"], bool(body.get("pinned", True)),
                        actor=body.get("coder") or CODER_NAME, note=body.get("note"))
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"ok": True})


@ROUTER.post("/api/storage/prune", json_body=True, required=["pairs"])
def post_storage_prune(req):
    body = req.body
    conflict = maintenance_conflict("prune")
    if conflict:
        return JsonResponse({"error": conflict}, status=400)
    keep_latest = _keep_latest_param(body.get("keep_latest"))
    try:
        pairs = [(p["model_version"], p["theme_id"]) for p in body["pairs"]]
    except (TypeError, KeyError):
        return JsonResponse({"error": "pairs must be a list of {model_version, theme_id}"}, status=400)
    if not pairs:
        return JsonResponse({"error": "nothing selected to prune"}, status=400)
    # Checked up front so a protected pair is a plain 400 rather than a failed job;
    # storage.prune_predictions() checks again at delete time.
    prunable, _kept = storage.plan_prune(
        model_runs.list_model_runs(), set(storage.list_pins()), keep_latest=keep_latest,
    )
    refused = [p for p in pairs if p not in prunable]
    if refused:
        return JsonResponse({
            "error": f"{len(refused)} selected run(s) are protected (latest, recent, or pinned) -- "
                     "refresh the Storage panel and try again.",
        }, status=400)
    job_id = PRUNE_JOBS.start({"pairs_done": 0, "pairs_total": len(pairs)}, run_prune_job, pairs, keep_latest)
    return JsonResponse({"job_id": job_id})


@ROUTER.get("/api/storage/prune_status")
def get_storage_prune_status(req):
    job = PRUNE_JOBS.get(req.query.get("job_id", ""))
    if job is None:
        return JsonResponse({"error": "unknown job_id"}, status=404)
    return JsonResponse(job)


@ROUTER.get("/api/storage/compact_check")
def get_storage_compact_check(req):
    return JsonResponse(storage.compaction_check())


@ROUTER.post("/api/storage/compact")
def post_storage_compact(req):
    conflict = maintenance_conflict("compact")
    if conflict:
        return JsonResponse({"error": conflict}, status=400)
    check = storage.compaction_check()
    if not check["ok"]:
        return JsonResponse({
            "error": f"Not enough free disk space to compact safely: needs about "
                     f"{check['required_bytes'] / 1e9:.2f} GB free, {check['available_bytes'] / 1e9:.2f} GB available.",
        }, status=400)
    job_id = COMPACT_JOBS.start({}, run_compact_job)
    return JsonResponse({"job_id": job_id})


@ROUTER.get("/api/storage/compact_status")
def get_storage_compact_status(req):
    job = COMPACT_JOBS.get(req.query.get("job_id", ""))
    if job is None:
        return JsonResponse({"error": "unknown job_id"}, status=404)
    return JsonResponse(job)


# --- Web Appendix ----------------------------------------------------------------

@ROUTER.get("/api/export/codebook")
def get_export_codebook(req):
    paths = activity_log.export_codebook(EXPORTS_DIR, scheme_id=req.query.get("scheme_id"))
    activity_log.log_activity(CODER_NAME, "codebook_export", {"files": [p.name for p in paths]})
    return JsonResponse({"exported_to": str(EXPORTS_DIR), "files": [p.name for p in paths]})


@ROUTER.get("/api/export/codebook_themes")
def get_export_codebook_themes(req):
    scheme_id = req.query.get("scheme_id") or ""
    if schemes.get_scheme(scheme_id) is None:
        return JsonResponse({"error": f"unknown scheme '{scheme_id}'"}, status=400)
    # Records come straight off disk with `turns` intact (get_dataset() strips them),
    # only for datasets whose segments appear in the export -- for turn_indices (K6).
    result = codebook_export.export_codebook_themes(
        EXPORTS_DIR, scheme_id, load_records=_load_dataset_records_with_turns, actor=CODER_NAME,
    )
    return JsonResponse({
        "exported_to": str(EXPORTS_DIR), "files": [p.name for p in result["paths"]],
        "codebook_version": result["codebook_version"], "label_rows": result["label_rows"],
        "turn_indices_available": result["turn_indices_available"], "completeness": result["completeness"],
    })


@ROUTER.get("/api/appendix/log")
def get_appendix_log(req):
    return JsonResponse(activity_log.get_appendix_feed(
        action_type=req.query.get("action_type"), since=req.query.get("since"),
    ))


@ROUTER.post("/api/appendix/filter_snapshot", json_body=True)
def post_appendix_filter_snapshot(req):
    activity_log.log_activity(
        req.body.get("coder") or CODER_NAME, "filter_snapshot", req.body.get("filters") or {},
    )
    return JsonResponse({"ok": True})


@ROUTER.get("/api/appendix/export")
def get_appendix_export(req):
    rows = activity_log.get_appendix_feed()
    html_body = appendix_export.render_html(rows).encode("utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return DownloadResponse(html_body, f"research_activity_log_{stamp}.html", "text/html; charset=utf-8")


# --- Projects ------------------------------------------------------------------------

@ROUTER.get("/api/projects")
def get_projects(req):
    return JsonResponse({
        "projects": project_registry.list_projects(),
        "active_path": str(CURRENT_PROJECT_DIR),
    })


@ROUTER.post("/api/projects/open", json_body=True)
def post_projects_open(req):
    new_path = (req.body.get("path") or "").strip()
    if not new_path:
        return JsonResponse({"error": "path is required"}, status=400)
    error = switch_project(new_path)
    if error:
        return JsonResponse({"error": error}, status=400)
    name = (req.body.get("name") or "").strip()
    if name:
        project_registry.register_project(new_path, name=name)
    return JsonResponse({"ok": True})


@ROUTER.post("/api/projects/remove", json_body=True)
def post_projects_remove(req):
    try:
        project_registry.remove_project(req.body.get("path") or "", active_path=CURRENT_PROJECT_DIR)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"ok": True})


@ROUTER.post("/api/projects/browse_folder")
def post_projects_browse_folder(req):
    try:
        chosen = browse_for_folder()
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    return JsonResponse({"path": chosen})


# --- Import: interview transcript (Search & Export tab's Import panel) ---------------

@ROUTER.post("/api/import/transcript/parse", json_body=True, required=["filename", "content_base64"])
def post_import_transcript_parse(req):
    body = req.body
    filename = (body.get("filename") or "").strip()
    content_b64 = body.get("content_base64")
    suffix = Path(filename).suffix.lower()
    if suffix not in (".docx", ".txt", ".json"):
        return JsonResponse({"error": f"unsupported file type '{suffix}' -- use .docx, .txt, or .json"}, status=400)
    try:
        raw = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError):
        return JsonResponse({"error": "content_base64 isn't valid base64"}, status=400)

    token = uuid.uuid4().hex
    if suffix == ".json":
        try:
            records = json.loads(raw.decode("utf-8"))
            query_api.validate_dataset_records(records, filename, require_item_id=False)
        except (json.JSONDecodeError, UnicodeDecodeError, query_api.DatasetValidationError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        records = interview_importer.normalize_json_records(records)
        label = (records[0].get("item_title") if records else None) or Path(filename).stem
        with PENDING_IMPORTS_LOCK:
            PENDING_IMPORTS[token] = {"kind": "json", "records": records, "filename": filename}
        return JsonResponse({
            "upload_token": token, "kind": "json",
            "n_records": len(records), "label": label,
        })

    try:
        lines = interview_importer.extract_lines_from_bytes(raw, suffix)
    except UnicodeDecodeError:
        return JsonResponse({"error": f"couldn't decode {filename} as UTF-8 text"}, status=400)
    except Exception as exc:  # a malformed .docx -- python-docx raises assorted errors
        return JsonResponse({"error": f"couldn't read {filename}: {exc}"}, status=400)
    turns, format_name = interview_importer.parse_transcript_turns(lines)
    if not turns:
        return JsonResponse({
            "error": "No turns found -- is this a Word Transcribe-style transcript "
                     "(\"HH:MM:SS Speaker N\" lines) or a speaker-labeled transcript "
                     "(\"Speaker: text\" lines, hand-typed or from ChatGPT/Claude)? See "
                     "docs/IMPORTING_DATA.md for examples of both. If your transcript is neither, "
                     "convert it to canonical JSON first (see docs/IMPORTING_DATA.md's LLM prompt "
                     "template) and upload that instead.",
        }, status=400)
    turns = interview_importer.bridge_backchannels(turns)
    labels = sorted({t["speaker_label"] for t in turns if t["speaker_label"]})
    unlabeled_count = sum(1 for t in turns if t["speaker_label"] is None)
    with PENDING_IMPORTS_LOCK:
        PENDING_IMPORTS[token] = {"kind": "transcript", "turns": turns, "filename": filename}
    return JsonResponse({
        "upload_token": token, "kind": "transcript",
        "labels": labels, "unlabeled_count": unlabeled_count, "n_turns": len(turns),
        "suggested_item_title": Path(filename).stem, "format_detected": format_name,
    })


@ROUTER.post("/api/import/transcript/commit", json_body=True)
def post_import_transcript_commit(req):
    body = req.body
    token = body.get("upload_token")
    with PENDING_IMPORTS_LOCK:
        pending = PENDING_IMPORTS.pop(token, None) if token else None
    if pending is None:
        return JsonResponse({"error": "unknown or expired upload_token -- re-select the file"}, status=400)

    if pending["kind"] == "reddit":
        return JsonResponse({"error": "wrong endpoint for this upload_token -- use /api/import/reddit/commit"}, status=400)
    elif pending["kind"] == "json":
        records = pending["records"]
        label = (records[0].get("item_title") if records else None) or Path(pending["filename"]).stem
    else:
        turns = pending["turns"]
        label_roles = body.get("label_roles") or {}
        unlabeled_role = body.get("unlabeled_role") or "other"
        interview_importer.assign_roles(turns, label_roles, unlabeled_role)

        person_name = (body.get("person_name") or "").strip()
        if not person_name:
            return JsonResponse({"error": "person_name is required"}, status=400)
        group_name = (body.get("group_name") or "").strip() or None
        person_title = (body.get("person_title") or "").strip() or None
        item_title = (body.get("item_title") or "").strip() or Path(pending["filename"]).stem
        publish_date = (body.get("publish_date") or "").strip() or None
        item_id = (body.get("item_id") or "").strip() or None

        record = interview_importer.build_record(
            turns, group_name=group_name, person_name=person_name, person_title=person_title,
            item_title=item_title, publish_date=publish_date, item_id=item_id,
        )
        records = [record]
        label = item_title

    try:
        query_api.validate_dataset_records(records, pending["filename"], require_item_id=True)
    except query_api.DatasetValidationError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    target_dataset_id = (body.get("target_dataset_id") or "").strip() or None
    if target_dataset_id:
        result = _append_dataset(
            records, target_dataset_id=target_dataset_id, expected_kind="interview_import",
            wrong_kind_message="can only add to an existing interview-transcript dataset, not a live download",
            action_type="interview_import", action_details={"filename": pending["filename"]},
        )
        return JsonResponse(result)

    result = _create_dataset(
        records, filename_prefix="interview", registry_kind="interview_import", label=label,
        dataset_label=f"{label} — {len(records)} interview(s)", segment=True,
        action_type="interview_import", action_details={"filename": pending["filename"], "label": label},
    )
    return JsonResponse(result)


# --- Import: Reddit / Arctic Shift ----------------------------------------------------

@ROUTER.post("/api/import/raw_upload", raw_body=True)
def post_import_raw_upload(req):
    filename = req.query.get("filename", "")
    try:
        text = req.raw_body.decode("utf-8")
    except UnicodeDecodeError:
        return JsonResponse({"error": f"couldn't decode {filename or 'uploaded file'} as UTF-8 text"}, status=400)
    upload_id = uuid.uuid4().hex
    with RAW_UPLOADS_LOCK:
        RAW_UPLOADS[upload_id] = text
    return JsonResponse({"upload_id": upload_id, "filename": filename, "size": len(req.raw_body)})


@ROUTER.post(
    "/api/import/reddit/parse", json_body=True,
    required=["submissions_filename", "submissions_upload_id"],
)
def post_import_reddit_parse(req):
    body = req.body
    submissions_filename = (body.get("submissions_filename") or "").strip()
    submissions_upload_id = body.get("submissions_upload_id")
    comments_filename = (body.get("comments_filename") or "").strip() or None
    comments_upload_id = body.get("comments_upload_id")

    with RAW_UPLOADS_LOCK:
        submissions_raw = RAW_UPLOADS.pop(submissions_upload_id, None)
    if submissions_raw is None:
        return JsonResponse({"error": "unknown or expired submissions upload -- re-select the file"}, status=400)
    try:
        submissions = reddit_importer.parse_jsonl_text(submissions_raw)
    except json.JSONDecodeError as exc:
        return JsonResponse({"error": f"{submissions_filename} isn't valid JSONL (one JSON object per line): {exc}"}, status=400)
    if not submissions:
        return JsonResponse({"error": f"No submissions found in {submissions_filename}"}, status=400)

    comments = []
    if comments_upload_id:
        with RAW_UPLOADS_LOCK:
            comments_raw = RAW_UPLOADS.pop(comments_upload_id, None)
        if comments_raw is None:
            return JsonResponse({"error": "unknown or expired comments upload -- re-select the file"}, status=400)
        try:
            comments = reddit_importer.parse_jsonl_text(comments_raw)
        except json.JSONDecodeError as exc:
            return JsonResponse({"error": f"{comments_filename or 'comments file'} isn't valid JSONL (one JSON object per line): {exc}"}, status=400)

    records, orphans = reddit_importer.build_records(submissions, comments)
    try:
        query_api.validate_dataset_records(records, submissions_filename, require_item_id=True)
    except query_api.DatasetValidationError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    n_turns, subreddits, label = reddit_importer.summarize_records(records)
    token = uuid.uuid4().hex
    with PENDING_IMPORTS_LOCK:
        PENDING_IMPORTS[token] = {
            "kind": "reddit", "records": records,
            "filename": submissions_filename, "label": label,
        }
    return JsonResponse({
        "upload_token": token, "n_records": len(records), "n_turns": n_turns,
        "subreddits": subreddits, "orphaned_comments": orphans,
    })


@ROUTER.post("/api/import/reddit/commit", json_body=True)
def post_import_reddit_commit(req):
    token = req.body.get("upload_token")
    with PENDING_IMPORTS_LOCK:
        pending = PENDING_IMPORTS.pop(token, None) if token else None
    if pending is None or pending["kind"] != "reddit":
        return JsonResponse({"error": "unknown or expired upload_token -- re-select the file(s)"}, status=400)

    records = pending["records"]
    label = pending["label"]

    target_dataset_id = (req.body.get("target_dataset_id") or "").strip() or None
    if target_dataset_id:
        result = _append_dataset(
            records, target_dataset_id=target_dataset_id, expected_kind="reddit_import",
            wrong_kind_message="can only add to an existing Reddit-import dataset, not a live download",
            count_noun="submission(s)", action_type="reddit_import",
            action_details={"filename": pending["filename"]},
        )
        return JsonResponse(result)

    result = _create_dataset(
        records, filename_prefix="reddit", registry_kind="reddit_import", label=label,
        dataset_label=f"r/{label} — {len(records)} submission(s)", segment=True,
        action_type="reddit_import", action_details={"filename": pending["filename"], "label": label},
    )
    return JsonResponse(result)


# --- Duplicate detection -------------------------------------------------------------

@ROUTER.get("/api/duplicates/status")
def get_duplicates_status(req):
    dataset_id = req.query.get("dataset")
    if not dataset_id:
        return JsonResponse({"error": "dataset is required"}, status=400)
    return JsonResponse(duplicates.get_duplicate_status_for_dataset(dataset_id))


@ROUTER.post("/api/duplicates/scan", json_body="optional")
def post_duplicates_scan(req):
    dataset_ids = req.body.get("dataset_ids") or None
    job_id = DUPLICATE_JOBS.start(
        {"n_items_scanned": 0, "n_datasets": 0, "n_candidate_pairs": 0,
         "clusters_done": 0, "clusters_total": 0, "run_id": None},
        run_duplicate_job, dataset_ids,
    )
    return JsonResponse({"job_id": job_id})


@ROUTER.get("/api/duplicates/scan_status")
def get_duplicates_scan_status(req):
    job = DUPLICATE_JOBS.get(req.query.get("job_id", ""))
    if job is None:
        return JsonResponse({"error": "unknown job_id"}, status=404)
    return JsonResponse(job)


@ROUTER.post("/api/duplicates/override", json_body=True)
def post_duplicates_override(req):
    body = req.body
    dataset_id = body.get("dataset_id")
    item_id = body.get("item_id")
    action = body.get("action")
    if not dataset_id or not item_id or action not in ("exclude", "include"):
        return JsonResponse({"error": "dataset_id, item_id, and action ('exclude'|'include') are required"}, status=400)
    try:
        duplicates.add_duplicate_override(
            dataset_id, item_id, action,
            canonical_dataset_id=body.get("canonical_dataset_id"),
            canonical_item_id=body.get("canonical_item_id"),
            actor=body.get("coder") or CODER_NAME,
            note=body.get("note"),
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"ok": True})


@ROUTER.post("/api/duplicates/override/remove", json_body=True, required=["dataset_id", "item_id"])
def post_duplicates_override_remove(req):
    body = req.body
    duplicates.remove_duplicate_override(body["dataset_id"], body["item_id"], actor=body.get("coder") or CODER_NAME)
    return JsonResponse({"ok": True})


@ROUTER.post("/api/datasets/status", json_body=True)
def post_datasets_status(req):
    body = req.body
    dataset_id = body.get("dataset_id")
    status = body.get("status")
    if not dataset_id or status not in ("active", "excluded", "unloaded"):
        return JsonResponse(
            {"error": "dataset_id and status ('active'|'excluded'|'unloaded') are required"}, status=400
        )
    if dataset_id not in {d["id"] for d in _all_datasets()}:
        return JsonResponse({"error": f"unknown dataset '{dataset_id}'"}, status=404)
    actor = body.get("coder") or CODER_NAME
    if status == "active":
        dataset_status_store.clear_dataset_status(dataset_id, actor=actor)
    else:
        dataset_status_store.set_dataset_status(dataset_id, status, actor=actor, note=body.get("note"))
    return JsonResponse({"ok": True, "dataset_id": dataset_id, "status": status})


# --- Analytics -------------------------------------------------------------------

@ROUTER.get("/api/analytics/corpus/summary")
def get_analytics_summary(req):
    segment_rows, duplicate_run_id, excluded_dataset_ids = corpus_analytics.get_corpus_scope()
    item_meta = _build_item_meta(segment_rows)
    summary = corpus_analytics.summary_metrics(
        segment_rows, item_meta, field=req.query.get("field"), value=req.query.get("value"),
    )
    summary["duplicate_run_id"] = duplicate_run_id
    summary["excluded_dataset_ids"] = sorted(excluded_dataset_ids)
    return JsonResponse(summary)


@ROUTER.get("/api/analytics/corpus/fields")
def get_analytics_fields(req):
    segment_rows, _run_id, _excluded = corpus_analytics.get_corpus_scope()
    item_meta = _build_item_meta(segment_rows)
    return JsonResponse(corpus_analytics.discover_fields(segment_rows, item_meta))


@ROUTER.get("/api/analytics/corpus/breakdown")
def get_analytics_breakdown(req):
    field = req.query.get("field", "")
    segment_rows, _run_id, _excluded = corpus_analytics.get_corpus_scope()
    item_meta = _build_item_meta(segment_rows)
    available = {f["field"] for f in corpus_analytics.discover_fields(segment_rows, item_meta)}
    if field not in available:
        return JsonResponse({"error": f"unknown or empty breakdown field '{field}'"}, status=400)
    return JsonResponse(corpus_analytics.field_breakdown(segment_rows, item_meta, field))


@ROUTER.get("/api/analytics/corpus/vocab_overlap")
def get_analytics_vocab_overlap(req):
    field = req.query.get("field", "")
    segment_rows, _run_id, _excluded = corpus_analytics.get_corpus_scope()
    item_meta = _build_item_meta(segment_rows)
    fields_by_name = {f["field"]: f for f in corpus_analytics.discover_fields(segment_rows, item_meta)}
    if fields_by_name.get(field, {}).get("type") != "categorical":
        return JsonResponse({"error": f"'{field}' is not a categorical breakdown field"}, status=400)
    return JsonResponse(corpus_analytics.vocabulary_overlap(segment_rows, item_meta, field))


class Handler(BaseHTTPRequestHandler):
    """Thin http.server glue: turns a raw request into (method, path, query, headers) for
    Router.dispatch, then writes back whatever Response it returns. All route logic above
    lives in plain (Request) -> Response functions, not on this class."""

    def _read_raw_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        response = ROUTER.dispatch(method, parsed.path, parse_qs(parsed.query), self.headers, self._read_raw_body)
        if response is None:
            self.send_error(404)
            return
        write_response(self, response)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, fmt, *args):
        pass  # keep the console quiet


MAX_PORT_ATTEMPTS = 5  # tried in order: the requested port, then the next 4 -- lets
                        # start.bat/start.command's launch just work if a prior run's
                        # server (or anything else) is still holding the default port,
                        # instead of crashing with an unexplained address-in-use error.

# HTTPServer (and so ThreadingHTTPServer) sets allow_reuse_address = True by default,
# which sets SO_REUSEADDR on the listening socket. On Windows, unlike Linux, that
# flag lets a second process bind onto a port another process is still actively
# LISTENING on -- confirmed on this machine: with it left at the default, the retry
# loop below never saw an OSError at all when the port was already taken, it just
# silently bound "successfully" alongside the other listener, and which process
# actually received a given connection was undefined. Disabling it is what makes an
# occupied port actually raise OSError, which is the whole point of the loop below.
ThreadingHTTPServer.allow_reuse_address = False


def main():
    requested_port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765

    server = None
    tried = []
    for port in range(requested_port, requested_port + MAX_PORT_ATTEMPTS):
        tried.append(port)
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue

    if server is None:
        print(
            f"Couldn't bind to any port in {tried[0]}-{tried[-1]} -- "
            f"something else on this machine is already using all of them. "
            f"Close whatever that is, or run with an explicit free port: "
            f"python viewer_server.py <port>"
        )
        sys.exit(1)

    print(f"Serving at http://127.0.0.1:{port}/  (Ctrl+C to stop)")
    webbrowser.open(f"http://127.0.0.1:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
