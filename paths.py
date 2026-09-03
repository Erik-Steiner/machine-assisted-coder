"""Shared project-data locations, so a researcher can run a second, independent
project from this same clone by pointing PROJECT_DIR at a different folder --
without a second git clone and without any in-app project switcher. Every
script that reads/writes queries/, coding.db, or exports/ imports these
constants rather than hardcoding its own `HERE / "queries"`-style path, so
there's one place to look and no risk of two files quietly disagreeing about
where "the project" lives.

demo_dataset.json and executive_interviews.json (the legacy primary dataset)
are deliberately NOT anchored here -- they're bundled/app-level convenience
files shipped with the repo itself, not a project's own research data, so
they stay relative to this file's own location regardless of PROJECT_DIR.
"""
import os
from pathlib import Path

HERE = Path(__file__).parent

PROJECT_DIR = Path(os.getenv("PROJECT_DIR") or HERE)
QUERIES_DIR = PROJECT_DIR / "queries"
EXPORTS_DIR = PROJECT_DIR / "exports"
DB_PATH = PROJECT_DIR / "coding.db"
