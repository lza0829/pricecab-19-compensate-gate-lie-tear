from __future__ import annotations

import sqlite3
from typing import Any

from app import config


def connect(*, immediate: bool = False) -> sqlite3.Connection:
    # Read the path lazily so tests can point DATA_DIR at a temp directory
    # after import by reloading app.config.
    conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if immediate:
        conn.execute("BEGIN IMMEDIATE")
    return conn


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None
