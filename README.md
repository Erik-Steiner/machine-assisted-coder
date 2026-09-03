# Executive Interview Viewer

A local research tool for browsing, importing, and qualitatively coding a corpus of text —
interview transcripts, Reddit/forum threads, or any other speaker-turn data (see
[IMPORTING_DATA.md](IMPORTING_DATA.md) for bringing in your own). It has six tabs:

- **Browse** — shows one item at a time with its metadata (group, person, date, views, etc.),
  lets you step forward and backward through items in chronological order, filter down to a
  focused set, and copy a snippet of text along with a citation tag telling you exactly which
  item it came from.
- **Search & Export** — look up a company or CEO/executive by name against the live
  ceointerviews.ai API, pick the right match from the results, and download that person's or
  company's transcripts as JSON + CSV. The result becomes a new dataset you can immediately
  switch to and browse, right alongside your other datasets — no restart needed.
- **Coding** — build a codebook of themes and apply them to speaker-turn segments of each item
  (segments are precomputed from the text, ~120 words each). Keyboard-driven: arrow keys move
  between segments, number keys 1–9 toggle a theme on the focused segment. Interviewer/moderator
  turns (if your data has them) display for context but aren't coded; threaded data (Reddit)
  renders indented by reply depth.
- **Model** — train a classifier (TF-IDF + logistic regression) per codebook theme on the codes
  you've applied so far, then monitor it: precision/recall/F1/PR-AUC and their history across
  training runs, the terms most predictive of each theme, and the highest-scored segments the
  model thinks belong to it (whether or not you've coded them yet). Read-only — it doesn't add
  or change codes.
- **Review** — pick one or more themes and get a cross-interview queue of every segment coded
  under them, plus segments the trained model flags highly for them that you haven't coded yet.
  Built for revisiting/refining a theme's boundaries once your codebook has matured, not for
  first-pass coding.
- **Web Appendix** — a chronological log of research actions (themes created/edited/merged,
  classifier training runs and their parameters, data imports, codebook exports, and the filter
  state active at a citation or export) — downloadable as a self-contained HTML file to attach as
  supplementary material to a manuscript.

It's built to be simple: Python scripts serve and process the data, and plain HTML/JS/CSS pages
display it. No frameworks, no build tools — just the packages in `requirements.txt`.

If you're a developer (or an AI coding agent) working on this codebase, read
[DEVELOPMENT.md](DEVELOPMENT.md) for the technical architecture and conventions, and
[CLAUDE.md](CLAUDE.md) for a fast-orientation summary and hard rules to follow.

## What's in this repo

**Browse / Search & Export (flat JSON datasets, in-memory)**
- `download_script.py` — bulk-downloads transcripts for one or more companies you name (as
  command-line arguments, or listed in your own `companies.txt` — see `companies.example.txt`)
  from the source API, one export per company, saved into `queries/`. This is how you build your
  starting datasets if you have ceointerviews.ai credentials.
- `scripts/import_interview_transcript.py`, `scripts/import_reddit.py` — import your own data
  (interview transcripts, Reddit/Arctic Shift exports) instead — see
  [IMPORTING_DATA.md](IMPORTING_DATA.md).
- `query_api.py` — the shared API client for ceointerviews.ai (keyword search, paginated feed
  fetch, row building) and the shared `queries/` export format (JSON + CSV + registry, and the
  canonical record-shape validator every import path uses). Used by `download_script.py`, both
  importers, and `viewer_server.py`'s Search & Export tab.
- `viewer_server.py` — the local web server: serves the dashboard, every registered dataset,
  live search results, on-demand query exports, and the Coding/Model/Review/Web Appendix
  endpoints.
- `viewer_static/viewer.html`, `app.js`, `styles.css` — the Browse / Search & Export frontend.
- `paths.py` — the shared `queries/`/`coding.db`/`exports/` locations for the project active *at
  startup* (`PROJECT_DIR`, see the setup section below).
- `project_registry.py`, `viewer_static/projects.js` — the in-app **Project** picker: lets you
  open, create, or remove projects from the running dashboard, no restart needed.

**Coding / Model / Review / Web Appendix (SQLite, `coding.db`)**
- `segmentation.py` — turns one item's `turns` into stable-ID'd, speaker-turn coding segments.
- `scripts/build_segments.py` — one-off/idempotent script that reads datasets from `queries/`
  and populates `coding.db`'s `segments` table. Run it (or re-run it) after adding new items to
  a dataset that feeds the Coding tab.
- `coding_store.py` — all SQLite access: segments, the codebook (themes), codes, classifier
  runs/predictions/top-terms, and the Web Appendix's activity log.
- `classifier.py` — the TF-IDF + logistic-regression classifier and its shared training pass.
- `scripts/train_classifiers.py` — CLI wrapper to train/retrain from the command line instead
  of the Model tab's "Train / retrain" button.
- `scripts/make_demo_dataset.py` — one-off script that samples a small real dataset down to
  `demo_dataset.json`, the bundled sample described above. You don't need to run this yourself
  unless you want to regenerate or replace the bundled sample.
- `scripts/migrate_field_names.py` — a one-time schema-rename migration; you won't need to run
  this on a fresh clone, only if you're upgrading a pre-existing `queries/`/`coding.db` from an
  older version of this repo.
- `appendix_export.py` — renders the Web Appendix tab's activity log into a self-contained HTML
  file.
- `viewer_static/coding.js`, `model.js`, `review.js`, `appendix.js` — the Coding/Model/Review/Web
  Appendix frontend, loaded after `app.js` in that order; each shares state with the ones before
  it rather than duplicating navigation logic.

**Other**
- `requirements.txt` — the Python packages the server, scripts, and demo notebooks need.
- `.env.example` — a template for the ceointerviews.ai API credentials (and the optional
  `PROJECT_DIR`/`CODER_NAME` settings).

The actual data is **not** included in this repo — it's large, and you generate it yourself:
- `queries/` — created automatically by `download_script.py` or the Search & Export tab; holds
  every dataset you've fetched (JSON + CSV per query, plus an `_index.json` registry) so they're
  still there next time you start the server. This is where your datasets normally live.
- `executive_interviews.json` — an older, single-file dataset format. If present at the repo
  root, `viewer_server.py` still loads it at startup as a `primary` dataset, for backwards
  compatibility, but nothing in this repo generates it anymore — new setups should just use
  `queries/`.
- `demo_dataset.json` — a small (10-interview) real sample bundled with the repo (see
  `scripts/make_demo_dataset.py`), loaded at startup as a `demo` dataset so you can try Browse
  before setting up an API key or downloading anything of your own.
- `coding.db` — the Coding/Model/Review SQLite database (segments, codebook, codes, classifier
  runs). Regenerate the `segments` table any time with `scripts/build_segments.py`; your actual
  codebook and codes only exist here (and in whatever syncs this folder), so don't delete it
  casually.

## Setting this up on a new machine

This repo ships with a small real sample (`demo_dataset.json`, 10 interviews) so you can try
Browse right after step 3 below, before you have your own API key or data — steps 4-6 are what
you need for your own research data and for the Coding/Model/Review tabs.

### 1. Get the code

Clone this repo, or copy the files above onto the new machine.

### 2. Install Python

You need Python 3.9 or newer. Check what you have with:

```
python --version
```

If you don't have Python, download it from [python.org](https://www.python.org/downloads/).

### 3. Install the required packages

From the project folder, run:

```
pip install -r requirements.txt
```

This installs `requests`, `pandas`, `python-dotenv`, and `scikit-learn`. `pandas` is only used
by the demo notebooks in `Examples/`; everything the server and scripts need on their own is
`requests`, `python-dotenv` (for downloading/exporting data) and `scikit-learn` (for the Model
tab's classifier). The dashboard server's Browse/Search & Export endpoints use nothing beyond
the Python standard library.

### 4. Add your API credentials

Copy the example environment file and fill in your real values:

```
cp .env.example .env
```

Then open `.env` in a text editor and set:

```
API_KEY=your-actual-api-key
BASE_URL=your-actual-api-base-url
```

Never commit your `.env` file or share it — it holds your credentials. It's already excluded
from this repo via `.gitignore`.

If more than one person codes in the same `coding.db` (e.g. a research assistant and a PI), also
set `CODER_NAME` in `.env` to whatever name should be attached to the codes each of you applies —
otherwise everyone is recorded as `"researcher"` and codes from different people become
indistinguishable later.

**Running a second, independent project from this same clone:** click **Project: &lt;name&gt;** in
the top-left corner of the dashboard (once it's running) to open, create, or remove projects —
each one has its own `queries/`, `coding.db`, and `exports/`, completely separate from the
others. Switching reloads the page into the newly active project, live, no server restart needed.
Type a folder path or click **Browse…** for a native folder picker; a path that doesn't exist yet
is created as a new, empty project. This is the easiest way to switch projects — see
`.env`'s `PROJECT_DIR` below only if you specifically want to fix which project a given server
process starts on (e.g. a scripted/automated launch).

`PROJECT_DIR` in `.env` sets which project is active when the server *starts* (e.g.
`PROJECT_DIR=C:/research/project-two`) — leave it unset for the default (the repo folder itself).
It's not remembered across restarts by the in-app picker; each server process independently
starts on `PROJECT_DIR` (or the repo folder) every time, and the picker only changes what's active
*while that process is running*. This also means you can run two server processes on different
ports, each on a different project, at the same time, if you want.

### 5. Get your data in

If you have ceointerviews.ai credentials, download it:

Run it with the name(s) of whichever company or companies your research is about:

```
python download_script.py "Company Name" "Another Company"
```

Each name is resolved against the live API (the same lookup the Search & Export tab's UI uses);
if a name matches more than one company, the script prints the candidates and their `company_id`
so you can rerun with the exact name you meant. If you'd rather not retype the list every time,
copy `companies.example.txt` to `companies.txt` (one name per line — this file is gitignored, so
it's just yours) and run `python download_script.py` with no arguments.

This downloads every matched company's executives' transcripts and saves them into `queries/` as
their own dataset (JSON + CSV, plus an entry in `queries/_index.json`). Depending on how much
data there is, this can take a while — the script prints progress as it goes and checkpoints
partial results along the way, so it's safe to let it run in the background.

You don't have to use this script at all — the Search & Export tab in the dashboard does the same
lookup-and-download interactively, one company or executive at a time, if you'd rather not use
the command line.

**No ceointerviews.ai access?** Bring your own interview transcripts or Reddit/Arctic Shift data
instead — see [IMPORTING_DATA.md](IMPORTING_DATA.md) for the two importer scripts and the
canonical data format (and what to do if your data is neither of those two shapes).

If you only need to browse and search, you can stop here and start the dashboard (step 7). The
next step is only needed for the Coding/Model/Review tabs.

### 6. (Optional) Build coding segments

The Coding, Model, and Review tabs read from `coding.db`, not from `queries/` directly. Populate
it once with:

```
python scripts/build_segments.py
```

With no arguments this segments every dataset currently registered in `queries/_index.json` — so
running it right after step 5 with no arguments picks up everything you just downloaded. To
segment only specific datasets, pass their dataset ids explicitly:

```
python scripts/build_segments.py q_abc123 q_def456
```

(Dataset ids come from `queries/_index.json`, or from the dataset dropdown's underlying data.)
This is idempotent — safe to re-run any time a dataset grows with newly downloaded interviews.

### 7. Start the dashboard

Run:

```
python viewer_server.py
```

You'll see a message like:

```
No executive_interviews.json found yet — Browse will be empty until you run a query in Search & Export, or run download_script.py
Serving at http://127.0.0.1:8765/  (Ctrl+C to stop)
```

(If you've already run `download_script.py`, your datasets are in `queries/` and will show up in
the dataset dropdown once you open the page — that startup message about `executive_interviews.json`
only refers to the legacy single-file format and is safe to ignore.)

Open that address in your web browser. You should see the dashboard, loaded and ready to browse.

To stop the server, go back to the terminal and press `Ctrl+C`.

If port 8765 is already used by something else on your machine, run it on a different port
instead:

```
python viewer_server.py 9000
```

and open `http://127.0.0.1:9000/` instead.

## Using the dashboard

### Browse tab

- Use the **dataset dropdown** in the top bar to pick which dataset to browse — any dataset
  you've downloaded or exported, listed newest-registered first.
- Use the **Previous** / **Next** buttons (or the left/right arrow keys) to move through
  interviews in date order.
- Click **Filters** to narrow the list by group, person, role, publish date (drag either
  end of the date range slider), channel, interview title, interview transcript content, or
  minimum views/likes/score. "Interview contains" searches the full transcript text
  server-side (transcripts aren't loaded into the browser until you open one, so this is what
  makes a full-text search possible without that).
- Click any interview in the left-hand list to jump straight to it.
- To copy a piece of transcript with a citation attached, select the text with your mouse, then
  click **Copy selection + citation**. You'll get the quote plus a tag like
  `[Anthropic — Dario Amodei (CEO) — "Interview Title" — 2024-03-01 — Bloomberg — item_id:12345]`
  on your clipboard, ready to paste into your notes.

### Search & Export tab

1. Type a company name or a CEO/executive name and click **Search**. Matches appear in two
   columns — Companies and Executives/CEOs — so you can tell people and companies with similar
   names apart before committing to a download.
2. Click the result you want. A panel appears showing what you picked (with its `company_id` or
   `entity_id`), plus optional **After date** / **Before date** filters if you only want a
   specific window.
3. Click **Fetch & export transcripts**. This pages through the live API in the background —
   for a prolific speaker or a large company this can take a little while — and shows progress
   as it goes.
4. When it's done, the export is saved into `queries/` as both JSON and CSV, and it's
   immediately available in the Browse tab's dataset dropdown. Click **View this dataset in
   Browse** to jump straight there.

### Coding tab

1. Click **Codebook** to expand the theme panel, and add a theme (name + optional description
   of what counts as an example). Each theme gets a color and shows up as a chip on every
   segment.
2. Navigate interviews the same way as Browse (Previous/Next or arrow keys) — the two tabs share
   the same position in the current dataset. Segments for the current interview load below,
   one card per speaker turn.
3. Click a card to focus it, or use **j**/**k** (or arrow down/up) to move focus between
   segments without the mouse. With a segment focused, press a **number key 1–9** to toggle the
   corresponding theme (in the order the codebook lists them) on that segment. You can also just
   click a theme chip directly on any card.
4. Use **Edit**, **Merge**, or **Archive** on a theme row to rename/redescribe it, fold it into
   another theme (recoding every segment tagged with it, then archiving it), or retire it without
   losing its codes.
5. The progress line under the codebook panel shows how many segments are coded overall and
   which themes still need more codes before a classifier can be trained on them (25 minimum).
6. If your data has interviewer/moderator turns (tagged `speaker_role: "interviewer"` — see
   [IMPORTING_DATA.md](IMPORTING_DATA.md)), they display for context but don't get theme chips —
   they're excluded from coding and from the classifier's training corpus by default. Threaded
   data (Reddit) renders indented by reply depth. Use the **Min words** field to hide short
   segments (e.g. one-word Reddit comments) that aren't worth coding — this only hides them from
   view, it doesn't delete anything.

If an item's segments don't appear in the Coding tab, its dataset hasn't been segmented yet — run
`python scripts/build_segments.py` (see setup step 6).

### Model tab

1. Click **Train / retrain all eligible themes**. This retrains every theme with at least 25
   coded segments in one background pass and polls until done — there's no per-theme button.
2. Select a theme from the sidebar to see its latest metrics (precision/recall/F1/PR-AUC), the
   full history of every training run for that theme, the terms most positively/negatively
   associated with it, and its top-scored segments (exemplars), each flagged as already coded or
   not.
3. Click **View in Coding** on an exemplar to jump to that exact segment in the Coding tab.

Metrics below the ~150-positive-code mark are shown with a caution note — treat the ranking as
suggestive, not final, until a theme has more codes.

### Review tab

1. Check one or more themes. A queue builds below, split into **Currently coded** (every segment
   coded under any of the selected themes) and **Model-flagged, not yet coded** (each selected
   theme's top-scored segments that aren't coded under it yet).
2. Toggle theme chips on any card the same way as Coding. Codes added here are tagged
   `source: "recoded"` internally, so a later query can distinguish first-pass coding from
   recoding.
3. Use **View in Coding** to jump to a segment's full interview context.

This tab is for revisiting/refining an existing codebook (e.g. two themes turning out to
overlap), not for first-pass coding — do that in the Coding tab.

### Web Appendix tab

A read-only, chronological log of research actions: themes created/edited/archived/merged,
classifier training runs (with the hyperparameters used), data imports/downloads, codebook
exports, and the Browse filter state active at the moment of a citation copy or a download.
Click **Download appendix for manuscript** for a self-contained HTML file you can attach as
supplementary material — open it in a browser and use its native "Print to PDF" if you need a
PDF. **Download codebook (CSV/JSON)** separately exports the full codebook (themes + codes +
model runs) to `exports/`, for backup or as a data supplement.

## Security & data handling

This is a local research tool, not a hosted service — keep that in mind before you share it
beyond your own machine:

- The server only binds to `127.0.0.1` (localhost) and has no login/authentication on any
  endpoint. That's fine as long as it stays local; if you ever need to reach it from another
  machine, put a reverse proxy with its own authentication in front of it rather than changing
  the bind address.
- `.env` (your API key), `coding.db` (your codebook and codes), and everything under `queries/`
  (downloaded transcripts) are all excluded from git via `.gitignore` and never committed. Treat
  them as sensitive research data — subject to whatever data-use agreement or IRB approval covers
  the interviews you're studying — regardless of what git tracks.
- Before making a repository that already has history public, check that an earlier commit
  didn't slip one of those files in before `.gitignore` covered it:
  ```
  git log --all --full-history -- .env coding.db "queries/*"
  ```
  If that turns up anything, rotate the exposed credentials and scrub the history before
  publishing, rather than just deleting the file going forward (removing a file in a new commit
  does not remove it from history).

## Troubleshooting

**"API_KEY and BASE_URL must be set" error when running `download_script.py`**
Your `.env` file is missing, misnamed, or not in the same folder as the script. Double check
step 4 above.

**Browser shows "This site can't be reached"**
Make sure `viewer_server.py` is still running in a terminal window, and that you're using the
exact address it printed (including the port number).

**Dashboard loads but says "No interviews match the current filters"**
Click **Reset filters** in the filter panel.

**Search & Export: "Company search failed: get_companies kept returning 5xx"**
As of this writing, the live `get_companies` endpoint's `keyword` search is erroring
server-side on ceointerviews.ai itself — it's not something wrong with your setup (confirmed by
calling the API directly, bypassing this dashboard entirely). Executive/CEO keyword search is
unaffected. In the meantime, search for one of the company's executives instead — their result
card shows the company name and `company_id` too — or start a chat with ceointerviews.ai
support if you need it fixed sooner.

**Coding tab says "No segments found for this interview"**
That interview's dataset hasn't been segmented into `coding.db` yet. Run
`python scripts/build_segments.py` (see setup step 6) — with no arguments it picks up every
dataset in `queries/_index.json`, so a fresh download just needs a re-run with no arguments.

**Model tab says a theme is "Not trained yet"**
It needs at least 25 coded segments before a classifier is attempted at all (and ideally 150+
before you trust its ranking much). Keep coding in the Coding or Review tab, then retrain.
