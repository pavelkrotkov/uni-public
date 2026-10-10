from __future__ import annotations

import sqlite3
from pathlib import Path

from . import ROOT

DEFAULT_DB_PATH = ROOT / "univ.sqlite3"


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
