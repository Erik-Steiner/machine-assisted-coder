"""A small routing seam for viewer_server.py's Handler, replacing an 840-line if/elif
dispatch chain with a route table plus one shared request-handling adapter.

The problem this solves: every one of viewer_server.py's ~50 endpoints used to repeat the
same three steps inline -- parse the JSON body (400 on failure), check required fields are
present (400 on failure), send a JSON response -- and each repetition was a chance to get
the status code or error shape slightly wrong. Route handlers here are plain functions
(Request) -> Response, with no dependency on BaseHTTPRequestHandler, so the parsing/
validation/error-shaping steps live in exactly one place (Router.dispatch) instead of once
per handler.

Two things stay deliberately outside this module because they're viewer_server.py's
business, not routing's: writing bytes to the socket (write_response takes the live
http.server handler instance and drives its .send_response/.wfile.write) and any
domain-specific lookup that isn't a path param (e.g. dataset_id passed as a query string is
still looked up by hand in the handler that needs it).
"""
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


class ApiError(Exception):
    """Raise from inside a handler to short-circuit with a JSON error response --
    an alternative to constructing an error JsonResponse and returning it by hand."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


class Query:
    """Wraps urllib.parse.parse_qs's {name: [values]} so handlers write
    req.query.get('dataset', 'primary') instead of qs.get('dataset', ['primary'])[0]."""

    def __init__(self, raw):
        self._raw = raw

    def get(self, name, default=None):
        values = self._raw.get(name)
        return values[0] if values else default


@dataclass
class Request:
    method: str
    path_params: dict
    query: Query
    headers: Any
    body: Optional[dict] = None      # only set when the route declares json_body
    raw_body: Optional[bytes] = None  # only set when the route declares raw_body
    loaded: dict = field(default_factory=dict)  # path-param loader results, see Router.get/post


@dataclass
class JsonResponse:
    payload: Any
    status: int = 200


@dataclass
class FileResponse:
    path: Any  # pathlib.Path to an on-disk file; 404s (bare, not JSON) if missing


@dataclass
class DownloadResponse:
    body: bytes
    filename: str
    content_type: str


_PARAM_RE = re.compile(r"^<(\w+)>$")


class _Route:
    def __init__(self, method, pattern, handler, *, json_body, raw_body, required, loaders):
        self.method = method
        self.handler = handler
        self.json_body = json_body  # False | True (strict, 400 on bad JSON) | "optional" (lenient, {} on bad JSON)
        self.raw_body = raw_body
        self.required = required
        self.loaders = loaders  # {path_param_name: loader_fn(value) -> object|None}
        self._segments = pattern.strip("/").split("/") if pattern != "/" else [""]

    def match(self, path):
        parts = path.strip("/").split("/") if path != "/" else [""]
        if len(parts) != len(self._segments):
            return None
        params = {}
        for pat_seg, actual in zip(self._segments, parts):
            m = _PARAM_RE.match(pat_seg)
            if m:
                if not actual:
                    return None
                params[m.group(1)] = actual
            elif pat_seg != actual:
                return None
        return params


class Router:
    """A route table: register handlers with @router.get(pattern)/@router.post(pattern),
    then call router.dispatch(...) once per request. Patterns use <name> for a single
    non-empty path segment, e.g. "/api/codebook/themes/<theme_id>/update"."""

    def __init__(self):
        self._routes = []

    def get(self, pattern, **kw):
        return self._register("GET", pattern, **kw)

    def post(self, pattern, *, json_body=False, raw_body=False, required=(), loaders=None, **kw):
        return self._register("POST", pattern, json_body=json_body, raw_body=raw_body,
                               required=required, loaders=loaders, **kw)

    def _register(self, method, pattern, *, json_body=False, raw_body=False, required=(), loaders=None):
        def decorator(fn):
            self._routes.append(_Route(
                method, pattern, fn, json_body=json_body, raw_body=raw_body,
                required=tuple(required), loaders=loaders or {},
            ))
            return fn
        return decorator

    def dispatch(self, method, path, raw_query, headers, read_raw_body):
        """Finds the matching route and runs the shared parse/validate/load steps before
        calling its handler. Returns a Response, or None if no route matched (caller sends
        a bare 404 -- these are API routes, a missing one isn't a JSON-shaped situation)."""
        for route in self._routes:
            if route.method != method:
                continue
            params = route.match(path)
            if params is None:
                continue

            req = Request(method=method, path_params=params, query=Query(raw_query), headers=headers)

            for name, loader in route.loaders.items():
                value = loader(params[name])
                if value is None:
                    noun = name[:-3].replace("_", " ") if name.endswith("_id") else name
                    return JsonResponse({"error": f"unknown {noun} '{params[name]}'"}, status=404)
                req.loaded[name] = value

            if route.raw_body:
                req.raw_body = read_raw_body()
            elif route.json_body:
                raw = read_raw_body()
                try:
                    req.body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    if route.json_body == "optional":
                        req.body = {}
                    else:
                        return JsonResponse({"error": "invalid JSON body"}, status=400)
                if route.required:
                    missing = [f for f in route.required if not req.body.get(f)]
                    if missing:
                        joined = " and ".join(missing) if len(missing) < 3 else ", ".join(missing[:-1]) + f", and {missing[-1]}"
                        plural = "is" if len(missing) == 1 else "are"
                        return JsonResponse({"error": f"{joined} {plural} required"}, status=400)

            try:
                return route.handler(req)
            except ApiError as exc:
                return JsonResponse({"error": exc.message}, status=exc.status)
            except Exception as exc:
                return JsonResponse({"error": str(exc)}, status=500)

        return None


_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def write_response(handler, response):
    """Drives handler's http.server plumbing (.send_response/.send_header/.wfile.write) to
    emit a Response. `handler` just needs to look like a BaseHTTPRequestHandler -- this
    isn't the place that decides *what* to send, only how to put it on the wire."""
    if isinstance(response, JsonResponse):
        body = json.dumps(response.payload).encode("utf-8")
        handler.send_response(response.status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    elif isinstance(response, FileResponse):
        try:
            body = response.path.read_bytes()
        except FileNotFoundError:
            handler.send_error(404)
            return
        handler.send_response(200)
        handler.send_header("Content-Type", _CONTENT_TYPES.get(response.path.suffix, "application/octet-stream"))
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    elif isinstance(response, DownloadResponse):
        handler.send_response(200)
        handler.send_header("Content-Type", response.content_type)
        handler.send_header("Content-Length", str(len(response.body)))
        handler.send_header("Content-Disposition", f'attachment; filename="{response.filename}"')
        handler.end_headers()
        handler.wfile.write(response.body)
    else:
        raise TypeError(f"unknown response type: {type(response)!r}")
