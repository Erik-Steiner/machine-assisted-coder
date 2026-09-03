"""Local server for the qualitative-coding research webapp (interview
transcripts, Reddit/forum threads, or other researcher-supplied text --
see IMPORTING_DATA.md).

Six tabs, one server:

  Browse tab       — reads whichever dataset is selected and lets you page
                      through it. Every dataset lives in queries/, one JSON +
                      CSV pair per query, registered in queries/_index.json —
                      whether it was fetched by Search & Export below, by
                      download_script.py's bulk per-company runs, by one of
                      scripts/import_*.py, or by hand.
                      (If executive_interviews.json still exists at the repo
                      root from an older setup, it's also loaded at startup
                      as a one-off "primary" dataset, for backwards
                      compatibility. If demo_dataset.json exists -- a small
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
                      their CLI counterparts), and IMPORTING_DATA.md.
  Coding            — a researcher builds a codebook of themes and codes
                      speaker-turn segments (precomputed by
                      scripts/build_segments.py from each item's `turns`)
                      against it. Backed by coding.db (SQLite), a separate
                      store from the in-memory JSON datasets above — see
                      coding_store.py. Data can come from the live API
                      (Search & Export) or from scripts/import_*.py for
                      interview transcripts and Reddit/Arctic Shift data --
                      see IMPORTING_DATA.md.
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
  Web Appendix      — a chronological log of research actions (theme CRUD,
                      training runs with their hyperparameters, downloads,
                      codebook exports, and Browse filter snapshots taken at
                      a citation/export moment), backed by coding_store.py's
                      activity_log/model_run_params tables, downloadable as
                      a self-contained HTML file for a manuscript's
                      supplementary material. See coding_store.log_activity/
                      get_appendix_feed and appendix_export.py.

Endpoints:

    GET  /api/datasets                    -> list of browsable datasets (registry-backed entries include
                                              "kind", e.g. "interview_import", so the transcript importer
                                              can offer only those as append targets)
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

    GET  /api/codebook/themes                       -> list codebook themes
    POST /api/codebook/themes                       -> create a theme
    POST /api/codebook/themes/<id>/update            -> edit a theme's fields
    POST /api/codebook/themes/<id>/archive           -> soft-hide a theme
    POST /api/codebook/themes/<id>/restore           -> un-archive a theme
    POST /api/codebook/themes/<id>/merge_into         -> merge a theme's codes into another, archive it

    GET  /api/coding/segments?item_id=<id>           -> ordered segments + their codes
    POST /api/coding/codes                           -> apply a code to a segment
    POST /api/coding/codes/delete                    -> remove a code from a segment
    GET  /api/coding/progress?dataset=<id>           -> coding counts by theme/group
    GET  /api/coding/model_runs                      -> latest classifier metrics per theme
    GET  /api/coding/model_runs?theme_id=<id>        -> full model_runs history for one theme
    GET  /api/coding/review_candidates?theme_ids=<a,b>&predicted_limit=<30>
        -> recode queue: segments coded under theme(s) a/b, plus each theme's
           top-scored not-yet-coded predictions (see coding_store.get_review_candidates)

    POST /api/model/train                            -> kick off a background training pass
                                                         (all eligible themes, one shared model_version)
    GET  /api/model/train_status?job_id=..           -> poll a training job's progress/result
    GET  /api/model/interpret?theme_id=<id>          -> metrics + top terms + exemplar segments +
                                                         value-prop stat for a theme's latest model
                                                         (see classifier.py / scripts/train_classifiers.py)

    GET  /api/export/codebook                        -> writes themes/codes/model_runs to exports/
                                                         as JSON+CSV (backup / publication supplement)
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
                                                         written yet) and returns a preview: speaker labels found
                                                         (docx/txt) or record count (json) -- see
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

    POST /api/import/reddit/parse                     -> {submissions_filename, submissions_content_base64,
                                                         comments_filename?, comments_content_base64?} ->
                                                         parses an uploaded Arctic Shift submissions(+comments)
                                                         JSONL export (in memory, nothing written yet) and
                                                         returns a preview: item/turn counts, subreddits found,
                                                         orphaned-comment count -- see scripts/import_reddit.py,
                                                         the Search & Export tab's Import panel
    POST /api/import/reddit/commit                    -> {upload_token} -> finishes a previously-parsed upload
                                                         (no extra input needed -- Reddit data has no
                                                         interviewer/respondent role decision to make), writes it
                                                         into queries/, and segments it into coding.db immediately,
                                                         same as /api/import/transcript/commit

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

Run:
    python viewer_server.py [port]
Then open http://127.0.0.1:<port>/ in a browser (default port 8765).
"""
import base64
import binascii
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import appendix_export
import classifier
import coding_store
import paths as project_paths
import project_registry
import query_api
import scripts.import_interview_transcript as interview_importer
import scripts.import_reddit as reddit_importer
import segmentation

HERE = Path(__file__).parent
DATA_FILE = HERE / "executive_interviews.json"
DEMO_FILE = HERE / "demo_dataset.json"
STATIC_DIR = HERE / "viewer_static"
QUERIES_DIR = project_paths.QUERIES_DIR
EXPORTS_DIR = project_paths.EXPORTS_DIR
# This process's own active project -- deliberately NOT read from
# project_registry.py, since more than one server process can run at once
# against different projects (see paths.py's PROJECT_DIR); "active" only
# makes sense per-process. switch_project() reassigns this.
CURRENT_PROJECT_DIR = project_paths.PROJECT_DIR

CODER_NAME = os.getenv("CODER_NAME", coding_store.DEFAULT_CODER)

coding_store.init_db()
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
    """Return a loaded dataset dict, loading a saved query from disk on first access."""
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


def list_datasets():
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
    return out


# --- Background query/export jobs -------------------------------------------------

JOBS = {}
JOBS_LOCK = threading.Lock()


def run_query_job(job_id, kind, source_id, label, company_name, after_dt, before_dt):
    with JOBS_LOCK:
        job = JOBS[job_id]

    try:
        def on_page(total, has_next):
            with JOBS_LOCK:
                job["items_fetched"] = total
                job["pages_fetched"] += 1

        fetch_kwargs = dict(after_dt=after_dt or None, before_dt=before_dt or None, on_page=on_page)
        if kind == "company":
            items = query_api.fetch_feed(company_id=source_id, **fetch_kwargs)
        else:
            items = query_api.fetch_feed(entity_id=source_id, **fetch_kwargs)

        rows = [query_api.build_row(it, company_name) for it in items]

        created_at = datetime.now(timezone.utc).isoformat()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        base_name = f"{kind}_{query_api.slugify(label)}_{stamp}"
        json_path, csv_path = query_api.write_export(QUERIES_DIR, base_name, rows)

        date_bit = ""
        if after_dt or before_dt:
            date_bit = f" [{after_dt or '…'} → {before_dt or '…'}]"
        dataset_label = f"{label}{date_bit} — {len(rows)} interviews"
        dataset_id = f"q_{uuid.uuid4().hex[:10]}"

        query_api.register_export(
            QUERIES_DIR,
            dataset_id=dataset_id,
            label=dataset_label,
            kind=kind,
            source_id=source_id,
            created_at=created_at,
            count=len(rows),
            json_path=json_path,
            csv_path=csv_path,
        )
        coding_store.log_activity(CODER_NAME, "query_download", {
            "dataset_id": dataset_id, "kind": kind, "source_id": source_id, "label": label,
            "after": after_dt, "before": before_dt, "count": len(rows),
        })

        with JOBS_LOCK:
            job["status"] = "done"
            job["dataset_id"] = dataset_id
            job["dataset_label"] = dataset_label
            job["json_file"] = json_path.name
            job["csv_file"] = csv_path.name
    except Exception as exc:
        with JOBS_LOCK:
            job["status"] = "error"
            job["error"] = str(exc)


# --- Background classifier training jobs (Model tab) --------------------------------

TRAIN_JOBS = {}
TRAIN_JOBS_LOCK = threading.Lock()


def run_train_job(job_id):
    def on_progress(event):
        stage = event["stage"]
        if stage not in ("theme_start", "theme_skipped", "theme_done"):
            return
        with TRAIN_JOBS_LOCK:
            job = TRAIN_JOBS[job_id]
            job["themes_total"] = event["total"]
            job["current_theme"] = {"theme_id": event["theme_id"], "name": event["name"]}
            if stage == "theme_skipped":
                job["themes_done"] = event["index"] + 1
                job["skipped"].append({
                    "theme_id": event["theme_id"], "name": event["name"],
                    "n_pos": event["n_pos"], "needed": classifier.MIN_POSITIVES_ATTEMPT,
                })
            elif stage == "theme_done":
                job["themes_done"] = event["index"] + 1
                job["trained"].append({
                    "theme_id": event["theme_id"], "name": event["name"], **event["metrics"],
                })

    try:
        summary = classifier.run_training_pass(progress_callback=on_progress)
        with TRAIN_JOBS_LOCK:
            job = TRAIN_JOBS[job_id]
            if summary["error"]:
                job["status"] = "error"
                job["error"] = summary["error"]
                return
            job["status"] = "done"
            job["model_version"] = summary["model_version"]
            job["themes_total"] = summary["themes_total"]
            job["themes_done"] = summary["themes_total"]
            # authoritative final lists, in case an incremental update above was missed
            job["trained"] = [
                {"theme_id": r["theme_id"], "name": r["name"], **r["metrics"]}
                for r in summary["results"] if r["status"] == "trained"
            ]
            job["skipped"] = [
                {"theme_id": r["theme_id"], "name": r["name"], "n_pos": r["n_pos"],
                 "needed": classifier.MIN_POSITIVES_ATTEMPT}
                for r in summary["results"] if r["status"] == "skipped_insufficient_positives"
            ]
            coding_store.log_activity(CODER_NAME, "training_run", {
                "model_version": job["model_version"],
                "trained": [{"theme_id": t["theme_id"], "name": t["name"]} for t in job["trained"]],
                "skipped": [{"theme_id": t["theme_id"], "name": t["name"]} for t in job["skipped"]],
            })
    except Exception as exc:
        with TRAIN_JOBS_LOCK:
            TRAIN_JOBS[job_id]["status"] = "error"
            TRAIN_JOBS[job_id]["error"] = str(exc)


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
    coding_store.upsert_segments(all_segments)
    return len(all_segments)


# --- In-app project switching -------------------------------------------------------

PROJECT_SWITCH_LOCK = threading.Lock()


def switch_project(new_dir):
    """Live-switches the running server's active project (queries/coding.db/
    exports/) to new_dir, without a restart. Returns None on success, or an
    error string. See project_registry.py for the recent-projects list this
    backs, and paths.py for why demo_dataset.json/executive_interviews.json
    are untouched here -- they're repo-level, not project-specific."""
    global QUERIES_DIR, EXPORTS_DIR, CURRENT_PROJECT_DIR
    new_dir = Path(new_dir)

    with JOBS_LOCK:
        if any(j.get("status") == "running" for j in JOBS.values()):
            return "A download job is still running -- wait for it to finish before switching projects."
    with TRAIN_JOBS_LOCK:
        if any(j.get("status") == "running" for j in TRAIN_JOBS.values()):
            return "A training job is still running -- wait for it to finish before switching projects."

    with PROJECT_SWITCH_LOCK:
        try:
            new_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"Couldn't create/access {new_dir}: {exc}"

        QUERIES_DIR = new_dir / "queries"
        EXPORTS_DIR = new_dir / "exports"
        coding_store.set_db_path(new_dir / "coding.db")
        CURRENT_PROJECT_DIR = new_dir

        with DATASETS_LOCK:
            for key in [k for k in DATASETS if k not in ("primary", "demo")]:
                del DATASETS[key]

        project_registry.register_project(new_dir)
        coding_store.log_activity(CODER_NAME, "project_switch", {"path": str(new_dir)})
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


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_download(self, body_bytes, filename, content_type):
        """Like _send_file, but for generated (not on-disk) content the browser
        should save rather than render -- the Web Appendix's exported HTML."""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body_bytes)

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "viewer.html")
        elif path == "/app.js":
            self._send_file(STATIC_DIR / "app.js")
        elif path == "/styles.css":
            self._send_file(STATIC_DIR / "styles.css")
        elif path == "/coding.js":
            self._send_file(STATIC_DIR / "coding.js")
        elif path == "/model.js":
            self._send_file(STATIC_DIR / "model.js")
        elif path == "/review.js":
            self._send_file(STATIC_DIR / "review.js")
        elif path == "/appendix.js":
            self._send_file(STATIC_DIR / "appendix.js")
        elif path == "/projects.js":
            self._send_file(STATIC_DIR / "projects.js")
        elif path == "/import.js":
            self._send_file(STATIC_DIR / "import.js")

        elif path == "/api/datasets":
            self._send_json(list_datasets())

        elif path == "/api/index":
            dataset_id = qs.get("dataset", ["primary"])[0]
            dataset = get_dataset(dataset_id)
            if dataset is None:
                self._send_json({"error": f"unknown dataset '{dataset_id}'"}, status=404)
                return
            self._send_json(dataset["index"])

        elif path.startswith("/api/interview/"):
            dataset_id = qs.get("dataset", ["primary"])[0]
            dataset = get_dataset(dataset_id)
            if dataset is None:
                self._send_json({"error": f"unknown dataset '{dataset_id}'"}, status=404)
                return
            try:
                iid = int(path.rsplit("/", 1)[-1])
            except ValueError:
                self.send_error(400)
                return
            rec = dataset["by_id"].get(iid)
            if rec is None:
                self.send_error(404)
                return
            self._send_json(rec)

        elif path == "/api/transcript_search":
            dataset_id = qs.get("dataset", ["primary"])[0]
            term = (qs.get("q", [""])[0]).strip().lower()
            dataset = get_dataset(dataset_id)
            if dataset is None:
                self._send_json({"error": f"unknown dataset '{dataset_id}'"}, status=404)
                return
            if not term:
                self._send_json({"ids": []})
                return
            ids = [
                rid for rid, rec in dataset["by_id"].items()
                if term in (rec.get("transcript_text") or "").lower()
            ]
            self._send_json({"ids": ids})

        elif path == "/api/search/companies":
            keyword = (qs.get("keyword", [""])[0]).strip()
            if not keyword:
                self._send_json({"results": []})
                return
            try:
                results = query_api.search_companies(keyword)
            except query_api.ApiCredentialsError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
                return
            self._send_json({"results": results})

        elif path == "/api/search/entities":
            keyword = (qs.get("keyword", [""])[0]).strip()
            if not keyword:
                self._send_json({"results": []})
                return
            try:
                results = query_api.search_entities(keyword)
            except query_api.ApiCredentialsError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
                return
            self._send_json({"results": results})

        elif path == "/api/query/status":
            job_id = qs.get("job_id", [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                job = dict(job) if job else None
            if job is None:
                self._send_json({"error": "unknown job_id"}, status=404)
                return
            self._send_json(job)

        elif path == "/api/codebook/themes":
            include_archived = qs.get("include_archived", [""])[0] == "1"
            self._send_json(coding_store.list_themes(include_archived=include_archived))

        elif path == "/api/export/codebook":
            paths = coding_store.export_codebook(EXPORTS_DIR)
            coding_store.log_activity(CODER_NAME, "codebook_export", {"files": [p.name for p in paths]})
            self._send_json({"exported_to": str(EXPORTS_DIR), "files": [p.name for p in paths]})

        elif path == "/api/coding/segments":
            item_id = (qs.get("item_id", [""])[0]).strip()
            if not item_id:
                self._send_json({"error": "item_id is required"}, status=400)
                return
            segments = coding_store.get_segments_for_item(item_id)
            codes_by_segment = coding_store.get_codes_for_segment_ids(
                [s["segment_id"] for s in segments]
            )
            for s in segments:
                s["theme_ids"] = codes_by_segment.get(s["segment_id"], [])
            self._send_json({"item_id": item_id, "segments": segments})

        elif path == "/api/coding/progress":
            dataset_id = qs.get("dataset", [None])[0]
            self._send_json(coding_store.get_progress(dataset_id=dataset_id))

        elif path == "/api/coding/review_candidates":
            theme_ids = [t for t in (qs.get("theme_ids", [""])[0]).split(",") if t]
            if not theme_ids:
                self._send_json({"error": "theme_ids (comma-separated) is required"}, status=400)
                return
            try:
                predicted_limit = int(qs.get("predicted_limit", ["30"])[0])
            except ValueError:
                predicted_limit = 30
            self._send_json(coding_store.get_review_candidates(theme_ids, predicted_limit=predicted_limit))

        elif path == "/api/coding/model_runs":
            theme_id = qs.get("theme_id", [None])[0]
            if theme_id:
                self._send_json({"theme_id": theme_id, "history": coding_store.list_model_runs(theme_id)})
            else:
                self._send_json(coding_store.get_latest_model_runs())

        elif path == "/api/model/train_status":
            job_id = qs.get("job_id", [""])[0]
            with TRAIN_JOBS_LOCK:
                job = TRAIN_JOBS.get(job_id)
                job = dict(job) if job else None
            if job is None:
                self._send_json({"error": "unknown job_id"}, status=404)
                return
            self._send_json(job)

        elif path == "/api/model/interpret":
            theme_id = qs.get("theme_id", [""])[0]
            theme = coding_store.get_theme(theme_id)
            if theme is None:
                self._send_json({"error": f"unknown theme '{theme_id}'"}, status=404)
                return

            runs = coding_store.list_model_runs(theme_id)
            if not runs:
                progress = coding_store.get_progress()
                by_theme = progress["by_theme"].get(theme_id, {"name": theme["name"], "count": 0})
                self._send_json({
                    "theme_id": theme_id, "trained": False,
                    "progress": by_theme, "min_positives": classifier.MIN_POSITIVES_ATTEMPT,
                })
                return

            requested_version = qs.get("model_version", [None])[0]
            run = next((r for r in runs if r["model_version"] == requested_version), runs[0]) \
                if requested_version else runs[0]
            try:
                limit = int(qs.get("limit", ["15"])[0])
            except ValueError:
                limit = 15

            self._send_json({
                "theme_id": theme_id, "trained": True, "model_version": run["model_version"],
                "metrics": run, "trust_positives_floor": classifier.TRUST_POSITIVES,
                "value_prop": coding_store.get_value_prop(theme_id, run["model_version"], run["threshold"]),
                "top_terms": coding_store.get_top_terms(run["model_version"], theme_id),
                "exemplars": coding_store.get_top_predictions(theme_id, run["model_version"], limit=limit),
            })

        elif path == "/api/appendix/log":
            action_type = qs.get("action_type", [None])[0]
            since = qs.get("since", [None])[0]
            self._send_json(coding_store.get_appendix_feed(action_type=action_type, since=since))

        elif path == "/api/appendix/export":
            rows = coding_store.get_appendix_feed()
            html_body = appendix_export.render_html(rows).encode("utf-8")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            self._send_download(html_body, f"research_activity_log_{stamp}.html", "text/html; charset=utf-8")

        elif path == "/api/duplicates/status":
            dataset_id = qs.get("dataset", [None])[0]
            if not dataset_id:
                self._send_json({"error": "dataset is required"}, status=400)
                return
            self._send_json(coding_store.get_duplicate_status_for_dataset(dataset_id))

        elif path == "/api/projects":
            self._send_json({
                "projects": project_registry.list_projects(),
                "active_path": str(CURRENT_PROJECT_DIR),
            })

        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/query/start":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return

            kind = body.get("kind")
            source_id = body.get("id")
            label = (body.get("label") or "").strip()
            company_name = (body.get("company_name") or label).strip()
            after_dt = (body.get("after") or "").strip() or None
            before_dt = (body.get("before") or "").strip() or None

            if kind not in ("company", "entity") or not source_id or not label:
                self._send_json({"error": "kind ('company'|'entity'), id, and label are required"}, status=400)
                return

            job_id = uuid.uuid4().hex
            with JOBS_LOCK:
                JOBS[job_id] = {
                    "status": "running",
                    "kind": kind,
                    "label": label,
                    "items_fetched": 0,
                    "pages_fetched": 0,
                }
            threading.Thread(
                target=run_query_job,
                args=(job_id, kind, source_id, label, company_name, after_dt, before_dt),
                daemon=True,
            ).start()
            self._send_json({"job_id": job_id})

        elif path == "/api/codebook/themes":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return
            try:
                theme = coding_store.create_theme(
                    name=body.get("name", ""),
                    description=body.get("description", ""),
                    example_words=body.get("example_words", ""),
                    color=body.get("color") or "#6b7fd7",
                    actor=body.get("coder") or CODER_NAME,
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            self._send_json(theme, status=201)

        elif path.startswith("/api/codebook/themes/") and path.endswith("/update"):
            theme_id = path[len("/api/codebook/themes/"):-len("/update")].strip("/")
            if coding_store.get_theme(theme_id) is None:
                self._send_json({"error": f"unknown theme '{theme_id}'"}, status=404)
                return
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return
            self._send_json(coding_store.update_theme(theme_id, body, actor=body.get("coder") or CODER_NAME))

        elif path.startswith("/api/codebook/themes/") and path.endswith("/archive"):
            theme_id = path[len("/api/codebook/themes/"):-len("/archive")].strip("/")
            if coding_store.get_theme(theme_id) is None:
                self._send_json({"error": f"unknown theme '{theme_id}'"}, status=404)
                return
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                body = {}
            self._send_json(coding_store.set_theme_status(theme_id, "archived", actor=body.get("coder") or CODER_NAME))

        elif path.startswith("/api/codebook/themes/") and path.endswith("/restore"):
            theme_id = path[len("/api/codebook/themes/"):-len("/restore")].strip("/")
            if coding_store.get_theme(theme_id) is None:
                self._send_json({"error": f"unknown theme '{theme_id}'"}, status=404)
                return
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                body = {}
            self._send_json(coding_store.set_theme_status(theme_id, "active", actor=body.get("coder") or CODER_NAME))

        elif path.startswith("/api/codebook/themes/") and path.endswith("/merge_into"):
            theme_id = path[len("/api/codebook/themes/"):-len("/merge_into")].strip("/")
            if coding_store.get_theme(theme_id) is None:
                self._send_json({"error": f"unknown theme '{theme_id}'"}, status=404)
                return
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return
            target_theme_id = body.get("target_theme_id")
            target = coding_store.get_theme(target_theme_id) if target_theme_id else None
            if target is None:
                self._send_json({"error": "target_theme_id must reference an existing theme"}, status=400)
                return
            if target["status"] != "active":
                self._send_json({"error": "cannot merge into an archived theme"}, status=400)
                return
            try:
                merged = coding_store.merge_themes(theme_id, target_theme_id, actor=body.get("coder") or CODER_NAME)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            self._send_json(merged)

        elif path == "/api/coding/codes":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return
            segment_id = body.get("segment_id")
            theme_id = body.get("theme_id")
            if not segment_id or not theme_id:
                self._send_json({"error": "segment_id and theme_id are required"}, status=400)
                return
            coding_store.add_code(
                segment_id, theme_id, coder=body.get("coder") or CODER_NAME,
                source=body.get("source") or "manual", note=body.get("note"),
            )
            self._send_json({"ok": True})

        elif path == "/api/coding/codes/delete":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return
            segment_id = body.get("segment_id")
            theme_id = body.get("theme_id")
            if not segment_id or not theme_id:
                self._send_json({"error": "segment_id and theme_id are required"}, status=400)
                return
            coding_store.remove_code(segment_id, theme_id, coder=body.get("coder") or CODER_NAME)
            self._send_json({"ok": True})

        elif path == "/api/model/train":
            job_id = uuid.uuid4().hex
            with TRAIN_JOBS_LOCK:
                TRAIN_JOBS[job_id] = {
                    "status": "running", "themes_total": 0, "themes_done": 0,
                    "current_theme": None, "trained": [], "skipped": [], "model_version": None,
                }
            threading.Thread(target=run_train_job, args=(job_id,), daemon=True).start()
            self._send_json({"job_id": job_id})

        elif path == "/api/appendix/filter_snapshot":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return
            coding_store.log_activity(
                body.get("coder") or CODER_NAME, "filter_snapshot", body.get("filters") or {},
            )
            self._send_json({"ok": True})

        elif path == "/api/projects/open":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return
            new_path = (body.get("path") or "").strip()
            if not new_path:
                self._send_json({"error": "path is required"}, status=400)
                return
            error = switch_project(new_path)
            if error:
                self._send_json({"error": error}, status=400)
                return
            name = (body.get("name") or "").strip()
            if name:
                project_registry.register_project(new_path, name=name)
            self._send_json({"ok": True})

        elif path == "/api/projects/remove":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return
            try:
                project_registry.remove_project(body.get("path") or "", active_path=CURRENT_PROJECT_DIR)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            self._send_json({"ok": True})

        elif path == "/api/projects/browse_folder":
            try:
                chosen = browse_for_folder()
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
                return
            self._send_json({"path": chosen})

        elif path == "/api/import/transcript/parse":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return
            filename = (body.get("filename") or "").strip()
            content_b64 = body.get("content_base64")
            if not filename or not content_b64:
                self._send_json({"error": "filename and content_base64 are required"}, status=400)
                return
            suffix = Path(filename).suffix.lower()
            if suffix not in (".docx", ".txt", ".json"):
                self._send_json({"error": f"unsupported file type '{suffix}' -- use .docx, .txt, or .json"}, status=400)
                return
            try:
                raw = base64.b64decode(content_b64, validate=True)
            except (binascii.Error, ValueError):
                self._send_json({"error": "content_base64 isn't valid base64"}, status=400)
                return

            token = uuid.uuid4().hex
            if suffix == ".json":
                try:
                    records = json.loads(raw.decode("utf-8"))
                    query_api.validate_dataset_records(records, filename, require_item_id=False)
                except (json.JSONDecodeError, UnicodeDecodeError, query_api.DatasetValidationError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                records = interview_importer.normalize_json_records(records)
                label = (records[0].get("item_title") if records else None) or Path(filename).stem
                with PENDING_IMPORTS_LOCK:
                    PENDING_IMPORTS[token] = {"kind": "json", "records": records, "filename": filename}
                self._send_json({
                    "upload_token": token, "kind": "json",
                    "n_records": len(records), "label": label,
                })
            else:
                try:
                    lines = interview_importer.extract_lines_from_bytes(raw, suffix)
                except UnicodeDecodeError:
                    self._send_json({"error": f"couldn't decode {filename} as UTF-8 text"}, status=400)
                    return
                except Exception as exc:  # a malformed .docx -- python-docx raises assorted errors
                    self._send_json({"error": f"couldn't read {filename}: {exc}"}, status=400)
                    return
                turns = interview_importer.bridge_backchannels(interview_importer.parse_turns(lines))
                if not turns:
                    self._send_json({
                        "error": "No timestamped turns found -- is this a Word Transcribe-style transcript "
                                 "(\"HH:MM:SS Speaker N\" lines)? If not, convert it to canonical JSON first "
                                 "(see IMPORTING_DATA.md's LLM prompt template) and upload that instead.",
                    }, status=400)
                    return
                labels = sorted({t["speaker_label"] for t in turns if t["speaker_label"]})
                unlabeled_count = sum(1 for t in turns if t["speaker_label"] is None)
                with PENDING_IMPORTS_LOCK:
                    PENDING_IMPORTS[token] = {"kind": "transcript", "turns": turns, "filename": filename}
                self._send_json({
                    "upload_token": token, "kind": "transcript",
                    "labels": labels, "unlabeled_count": unlabeled_count, "n_turns": len(turns),
                    "suggested_item_title": Path(filename).stem,
                })

        elif path == "/api/import/transcript/commit":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return
            token = body.get("upload_token")
            with PENDING_IMPORTS_LOCK:
                pending = PENDING_IMPORTS.pop(token, None) if token else None
            if pending is None:
                self._send_json({"error": "unknown or expired upload_token -- re-select the file"}, status=400)
                return

            if pending["kind"] == "reddit":
                self._send_json({"error": "wrong endpoint for this upload_token -- use /api/import/reddit/commit"}, status=400)
                return
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
                    self._send_json({"error": "person_name is required"}, status=400)
                    return
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
                self._send_json({"error": str(exc)}, status=400)
                return

            target_dataset_id = (body.get("target_dataset_id") or "").strip() or None
            if target_dataset_id:
                entry = next((e for e in query_api.load_registry(QUERIES_DIR) if e["id"] == target_dataset_id), None)
                if entry is None:
                    self._send_json({"error": "unknown target dataset -- it may have been removed"}, status=400)
                    return
                if entry.get("kind") != "interview_import":
                    self._send_json(
                        {"error": "can only add to an existing interview-transcript dataset, not a live download"},
                        status=400,
                    )
                    return
                try:
                    _, updated_entry = query_api.append_to_dataset(QUERIES_DIR, target_dataset_id, records)
                except query_api.DatasetValidationError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                segments_added = segment_new_dataset(records, target_dataset_id)
                with DATASETS_LOCK:
                    DATASETS.pop(target_dataset_id, None)  # force a reload from disk next access
                coding_store.log_activity(CODER_NAME, "interview_import", {
                    "dataset_id": target_dataset_id, "label": updated_entry["label"], "count": len(records),
                    "filename": pending["filename"], "segments_added": segments_added, "appended": True,
                })
                self._send_json({
                    "dataset_id": target_dataset_id, "dataset_label": updated_entry["label"],
                    "json_file": updated_entry["json_file"], "csv_file": updated_entry["csv_file"],
                    "count": len(records), "segments_added": segments_added, "appended": True,
                })
                return

            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            base_name = f"interview_{query_api.slugify(label)}_{stamp}"
            json_path, csv_path = query_api.write_export(QUERIES_DIR, base_name, records)
            dataset_id = f"q_{uuid.uuid4().hex[:10]}"
            dataset_label = f"{label} — {len(records)} interview(s)"
            query_api.register_export(
                QUERIES_DIR,
                dataset_id=dataset_id,
                label=dataset_label,
                kind="interview_import",
                source_id=None,
                created_at=datetime.now(timezone.utc).isoformat(),
                count=len(records),
                json_path=json_path,
                csv_path=csv_path,
            )
            segments_added = segment_new_dataset(records, dataset_id)
            coding_store.log_activity(CODER_NAME, "interview_import", {
                "dataset_id": dataset_id, "label": label, "count": len(records),
                "filename": pending["filename"], "segments_added": segments_added,
            })
            self._send_json({
                "dataset_id": dataset_id, "dataset_label": dataset_label,
                "json_file": json_path.name, "csv_file": csv_path.name,
                "count": len(records), "segments_added": segments_added,
            })

        elif path == "/api/import/reddit/parse":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return
            submissions_filename = (body.get("submissions_filename") or "").strip()
            submissions_b64 = body.get("submissions_content_base64")
            if not submissions_filename or not submissions_b64:
                self._send_json({"error": "submissions_filename and submissions_content_base64 are required"}, status=400)
                return
            comments_filename = (body.get("comments_filename") or "").strip() or None
            comments_b64 = body.get("comments_content_base64")

            try:
                submissions_raw = base64.b64decode(submissions_b64, validate=True).decode("utf-8")
            except (binascii.Error, ValueError, UnicodeDecodeError):
                self._send_json({"error": f"couldn't decode {submissions_filename} as UTF-8 text"}, status=400)
                return
            try:
                submissions = reddit_importer.parse_jsonl_text(submissions_raw)
            except json.JSONDecodeError as exc:
                self._send_json({"error": f"{submissions_filename} isn't valid JSONL (one JSON object per line): {exc}"}, status=400)
                return
            if not submissions:
                self._send_json({"error": f"No submissions found in {submissions_filename}"}, status=400)
                return

            comments = []
            if comments_b64:
                try:
                    comments_raw = base64.b64decode(comments_b64, validate=True).decode("utf-8")
                except (binascii.Error, ValueError, UnicodeDecodeError):
                    self._send_json({"error": f"couldn't decode {comments_filename or 'comments file'} as UTF-8 text"}, status=400)
                    return
                try:
                    comments = reddit_importer.parse_jsonl_text(comments_raw)
                except json.JSONDecodeError as exc:
                    self._send_json({"error": f"{comments_filename or 'comments file'} isn't valid JSONL (one JSON object per line): {exc}"}, status=400)
                    return

            records, orphans = reddit_importer.build_records(submissions, comments)
            try:
                query_api.validate_dataset_records(records, submissions_filename, require_item_id=True)
            except query_api.DatasetValidationError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return

            n_turns, subreddits, label = reddit_importer.summarize_records(records)
            token = uuid.uuid4().hex
            with PENDING_IMPORTS_LOCK:
                PENDING_IMPORTS[token] = {
                    "kind": "reddit", "records": records,
                    "filename": submissions_filename, "label": label,
                }
            self._send_json({
                "upload_token": token, "n_records": len(records), "n_turns": n_turns,
                "subreddits": subreddits, "orphaned_comments": orphans,
            })

        elif path == "/api/import/reddit/commit":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return
            token = body.get("upload_token")
            with PENDING_IMPORTS_LOCK:
                pending = PENDING_IMPORTS.pop(token, None) if token else None
            if pending is None or pending["kind"] != "reddit":
                self._send_json({"error": "unknown or expired upload_token -- re-select the file(s)"}, status=400)
                return

            records = pending["records"]
            label = pending["label"]
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            base_name = f"reddit_{query_api.slugify(label)}_{stamp}"
            json_path, csv_path = query_api.write_export(QUERIES_DIR, base_name, records)
            dataset_id = f"q_{uuid.uuid4().hex[:10]}"
            dataset_label = f"r/{label} — {len(records)} submission(s)"
            query_api.register_export(
                QUERIES_DIR,
                dataset_id=dataset_id,
                label=dataset_label,
                kind="reddit_import",
                source_id=None,
                created_at=datetime.now(timezone.utc).isoformat(),
                count=len(records),
                json_path=json_path,
                csv_path=csv_path,
            )
            segments_added = segment_new_dataset(records, dataset_id)
            coding_store.log_activity(CODER_NAME, "reddit_import", {
                "dataset_id": dataset_id, "label": label, "count": len(records),
                "filename": pending["filename"], "segments_added": segments_added,
            })
            self._send_json({
                "dataset_id": dataset_id, "dataset_label": dataset_label,
                "json_file": json_path.name, "csv_file": csv_path.name,
                "count": len(records), "segments_added": segments_added,
            })

        elif path == "/api/duplicates/override":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return
            dataset_id = body.get("dataset_id")
            item_id = body.get("item_id")
            action = body.get("action")
            if not dataset_id or not item_id or action not in ("exclude", "include"):
                self._send_json({"error": "dataset_id, item_id, and action ('exclude'|'include') are required"}, status=400)
                return
            try:
                coding_store.add_duplicate_override(
                    dataset_id, item_id, action,
                    canonical_dataset_id=body.get("canonical_dataset_id"),
                    canonical_item_id=body.get("canonical_item_id"),
                    actor=body.get("coder") or CODER_NAME,
                    note=body.get("note"),
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            self._send_json({"ok": True})

        elif path == "/api/duplicates/override/remove":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return
            dataset_id = body.get("dataset_id")
            item_id = body.get("item_id")
            if not dataset_id or not item_id:
                self._send_json({"error": "dataset_id and item_id are required"}, status=400)
                return
            coding_store.remove_duplicate_override(dataset_id, item_id, actor=body.get("coder") or CODER_NAME)
            self._send_json({"ok": True})

        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass  # keep the console quiet


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Serving at http://127.0.0.1:{port}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
