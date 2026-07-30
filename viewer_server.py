"""Local server for the executive interview viewer webapp.

Loads executive_interviews.json once at startup, sorts every interview by
publish_date, and exposes it over two small JSON endpoints so the frontend
never has to pull the (large, ~70MB) transcript blob all at once:

    GET /api/index              -> metadata only (no transcript_text), for
                                    every interview, sorted by publish_date.
                                    Small payload; the frontend uses this for
                                    filtering, ordering, and the jump-list.
    GET /api/interview/<id>      -> full record (incl. transcript_text) for
                                    one interview, fetched on demand as the
                                    researcher navigates.

Run:
    python viewer_server.py [port]
Then open http://127.0.0.1:<port>/ in a browser (default port 8765).
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).parent
DATA_FILE = HERE / "executive_interviews.json"
STATIC_DIR = HERE / "viewer_static"


def _sort_key(rec):
    # Records without a publish_date sort last instead of raising/crashing.
    pd = rec.get("publish_date")
    return (1, "") if not pd else (0, pd)


def load_data():
    with open(DATA_FILE, encoding="utf-8") as f:
        records = json.load(f)
    records.sort(key=_sort_key)

    index = []
    by_id = {}
    for i, rec in enumerate(records):
        rec = dict(rec)
        rec.pop("enhanced_transcript_json", None)  # not used by the viewer
        rec["id"] = i
        by_id[i] = rec

        meta = dict(rec)
        meta.pop("transcript_text", None)  # keep the index payload light
        index.append(meta)
    return index, by_id


INDEX, BY_ID = load_data()
print(f"Loaded {len(INDEX)} interviews from {DATA_FILE.name}")

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

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "viewer.html")
        elif path == "/app.js":
            self._send_file(STATIC_DIR / "app.js")
        elif path == "/styles.css":
            self._send_file(STATIC_DIR / "styles.css")
        elif path == "/api/index":
            self._send_json(INDEX)
        elif path.startswith("/api/interview/"):
            try:
                iid = int(path.rsplit("/", 1)[-1])
            except ValueError:
                self.send_error(400)
                return
            rec = BY_ID.get(iid)
            if rec is None:
                self.send_error(404)
                return
            self._send_json(rec)
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
