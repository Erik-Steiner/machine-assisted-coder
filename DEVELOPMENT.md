# Development and Production Guide: Interview Viewer Dashboard

## Purpose

This dashboard shows one record at a time from a large JSON data file. A researcher moves through records in order, filters by metadata, and copies text with a citation tag. This guide explains how the dashboard works. Use it to build the same dashboard for a different dataset.

## Architecture

The dashboard has two parts: a server and a frontend.

The server is `viewer_server.py`. It uses only the Python standard library. It reads the data file once at startup. It splits each record into two views: a light "index" view (small metadata fields) and a full "detail" view (all fields, including large text fields). The server exposes two endpoints:

1. `GET /api/index` returns the index view for every record, sorted by a chosen field.
2. `GET /api/interview/<id>` returns the full detail view for one record.

The frontend is three static files in `viewer_static/`: `viewer.html`, `app.js`, and `styles.css`. The browser loads `viewer.html`, which loads `app.js`. The script fetches `/api/index` once and keeps it in memory. All filtering and sorting happen in the browser, using this small index. The script fetches the full record from `/api/interview/<id>` only when the researcher views that record. The script caches each fetched record so it does not fetch the same record twice.

## Why this design

A raw data file can hold gigabytes of text across thousands of records. Loading all of that text into the browser at once is slow. This design splits each record into a small part (metadata) and a large part (content). The browser only ever holds one dataset's worth of small metadata, plus the few full records the researcher has already viewed.

The server uses only the Python standard library. This means no install step and no dependency conflicts. Any machine with Python 3 can run it.

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
3. Open `viewer_server.py`. In `load_data()`, change the field name passed to `.pop()` for content fields, so the index view drops them.
4. In `_sort_key()`, change `publish_date` to your dataset's order field.
5. Open `viewer_static/app.js`. Update the `uniqueSorted()` calls in `buildFilterOptions()` to match your metadata field names.
6. Update `matchesFilters()` so each filter checks the correct field name.
7. Update `renderMeta()` and `buildCitationTag()` so the header and citation tag show your dataset's fields.
8. If your content field is not plain, line-broken text, update `renderTranscript()`.
9. Start the server and open the page in a browser. Confirm the index loads, filters narrow the list, and a detail record loads when selected.

## File reference

`viewer_server.py`
This file loads the data, builds the index, and serves both endpoints. Change this file when you add, rename, or remove a metadata or content field, or when you change the sort order.

`viewer_static/viewer.html`
This file defines the page layout: header, filter panel, jump list, metadata box, navigation buttons, and transcript box. Change this file when you add a new filter control or a new metadata field.

`viewer_static/app.js`
This file holds all frontend logic: fetching data, filtering, navigation, rendering, and the copy-with-citation feature. Most dataset-specific changes happen here.

`viewer_static/styles.css`
This file holds the visual style. It needs no change for a new dataset, unless the layout does not fit the new content.

## Local development workflow

1. Put the JSON data file in the same folder as `viewer_server.py`, or update `DATA_FILE` in `viewer_server.py` to point to it.
2. Run `python viewer_server.py [port]`. The default port is 8765.
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

- The server holds the full dataset in memory, in two forms: the raw record list and the index. Before you run the server, confirm your machine has enough RAM for your dataset.
- The server runs as a single process. `ThreadingHTTPServer` handles concurrent requests, but all requests share one in-memory copy of the data.
- The frontend has no automated tests. Test changes by hand in a browser.
- The frontend ships unminified. This is fine for local use and small teams. If load time in production matters, minify or bundle the files.

## Guidance for a Claude Code agent

1. Before you change `viewer_server.py` or `viewer_static/app.js`, read each file in full. Both files are short enough to read in one pass.
2. Keep the two-endpoint contract: a light index endpoint and a per-record detail endpoint. Do not merge them back into one large payload, since that removes the reason for this design.
3. Match field names exactly between the server's index payload and the frontend's filter code. A silent mismatch shows an empty filter list instead of an error.
4. Unless the user asks for a frontend framework or a build step, do not add one. The point of this pattern is that any agent can read and edit the whole frontend directly.
5. After a change, start the server and load the page in a real browser. Check the browser console for errors before you report the change as complete.
