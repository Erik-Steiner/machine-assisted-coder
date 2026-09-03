"""Tracks the researcher's known projects (each one a folder holding its own
queries/, coding.db, exports/ -- see paths.py) so the in-app "Open Project"
picker can show a recent-projects list instead of requiring a typed path
every time. Backed by projects_registry.json at the repo root -- gitignored,
local machine state, same spirit as coding.db itself.

This module never touches a project's own data (queries/, coding.db) -- it
only tracks *which folders* are known projects. It deliberately does NOT
track "the active project" here: this app supports running more than one
viewer_server.py process at once against different projects (see paths.py's
PROJECT_DIR), so "active" is inherently per-process state, not something a
single shared file could represent correctly -- one process switching would
otherwise silently corrupt what a *different* running process's UI reports
as active. Each server process tracks its own active project in its own
memory (viewer_server.py's switch_project()); this module is just the
shared list of folders underneath that.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
REGISTRY_PATH = HERE / "projects_registry.json"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _load():
    if not REGISTRY_PATH.exists():
        return {"projects": []}
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"projects": []}


def _save(data):
    REGISTRY_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _normalize(path):
    return str(Path(path).resolve())


def ensure_initialized(startup_active_path):
    """Called once at server startup. If no registry exists yet, creates one
    with a single entry for whatever project is active at startup (today's
    PROJECT_DIR resolution, or the repo root) -- so upgrading an existing
    setup is a no-op: the researcher's current project just shows up as
    already-known the first time they open the picker."""
    data = _load()
    if data["projects"]:
        return
    register_project(startup_active_path, name="Default project")


def list_projects():
    return _load()["projects"]


def register_project(path, name=None):
    """Adds a project if new, or refreshes last_opened (and renames it, if an
    explicit name was given) if already known. Returns the project's registry
    entry."""
    norm = _normalize(path)
    data = _load()
    for p in data["projects"]:
        if p["path"] == norm:
            p["last_opened"] = _now()
            if name:
                p["name"] = name
            _save(data)
            return p
    entry = {
        "path": norm,
        "name": name or Path(norm).name or norm,
        "created_at": _now(),
        "last_opened": _now(),
    }
    data["projects"].append(entry)
    _save(data)
    return entry


def remove_project(path, active_path=None):
    """Registry-only removal -- never touches the folder or its data. Refuses
    to remove active_path (the CALLING process's own currently-active
    project, passed in by viewer_server.py -- this module has no notion of
    "active" of its own, see the module docstring)."""
    norm = _normalize(path)
    if active_path is not None and norm == _normalize(active_path):
        raise ValueError("cannot remove the currently active project -- switch to a different one first")
    data = _load()
    data["projects"] = [p for p in data["projects"] if p["path"] != norm]
    _save(data)
