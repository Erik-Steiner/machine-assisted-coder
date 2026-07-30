# Executive Interview Viewer

A small local tool for browsing a large collection of executive interview transcripts. It shows one interview at a time with its metadata (company, executive, date, views, etc.), lets you step forward and backward through interviews in chronological order, filter down to a focused set, and copy a snippet of transcript text along with a citation tag telling you exactly which interview it came from.

It's built to be simple: a Python script that serves the data, and a plain HTML/JS/CSS page that displays it. No frameworks, no build tools, nothing to install beyond a few common Python packages.

If you're a developer (or an AI coding agent) who wants to adapt this same dashboard to a different dataset, see [DEVELOPMENT.md](DEVELOPMENT.md) for the technical guide.

## What's in this repo

- `download_script.py` — pulls interview data from the source API and saves it as `executive_interviews.json`.
- `viewer_server.py` — a small local web server that serves the dashboard and the interview data.
- `viewer_static/` — the dashboard itself (`viewer.html`, `app.js`, `styles.css`).
- `requirements.txt` — the Python packages `download_script.py` needs.
- `.env.example` — a template for the API credentials `download_script.py` needs.

The actual data file (`executive_interviews.json`) is **not** included in this repo — it's large, and it's something you generate yourself by running the download script.

## Setting this up on a new machine

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

This installs `requests`, `pandas`, and `python-dotenv` — everything `download_script.py` needs. The dashboard server itself (`viewer_server.py`) needs nothing extra; it only uses what's built into Python.

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

Never commit your `.env` file or share it — it holds your credentials. It's already excluded from this repo via `.gitignore`.

### 5. Download the interview data

Run:

```
python download_script.py
```

This talks to the source API and saves everything to `executive_interviews.json` in the same folder. Depending on how much data there is, this can take a while — the script prints progress as it goes and saves partial results along the way, so it's safe to let it run in the background.

### 6. Start the dashboard

Run:

```
python viewer_server.py
```

You'll see a message like:

```
Loaded 1171 interviews from executive_interviews.json
Serving at http://127.0.0.1:8765/  (Ctrl+C to stop)
```

Open that address in your web browser. You should see the dashboard, loaded and ready to browse.

To stop the server, go back to the terminal and press `Ctrl+C`.

If port 8765 is already used by something else on your machine, run it on a different port instead:

```
python viewer_server.py 9000
```

and open `http://127.0.0.1:9000/` instead.

## Using the dashboard

- Use the **Previous** / **Next** buttons (or the left/right arrow keys) to move through interviews in date order.
- Click **Filters** to narrow the list by company, executive, title, year, channel, or minimum views/likes/score.
- Click any interview in the left-hand list to jump straight to it.
- To copy a piece of transcript with a citation attached, select the text with your mouse, then click **Copy selection + citation**. You'll get the quote plus a tag like `[Anthropic — Dario Amodei (CEO) — "Interview Title" — 2024-03-01 — feed_item_id:12345]` on your clipboard, ready to paste into your notes.

## Troubleshooting

**"API_KEY and BASE_URL must be set" error when running `download_script.py`**
Your `.env` file is missing, misnamed, or not in the same folder as the script. Double check step 4 above.

**Browser shows "This site can't be reached"**
Make sure `viewer_server.py` is still running in a terminal window, and that you're using the exact address it printed (including the port number).

**Dashboard loads but says "No interviews match the current filters"**
Click **Reset filters** in the filter panel.
