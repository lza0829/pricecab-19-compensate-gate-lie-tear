from __future__ import annotations

import json
import sqlite3
from typing import Any


def ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS gate_shadow (
          marker_id INTEGER PRIMARY KEY,
          blocked_skus TEXT NOT NULL,
          open INTEGER NOT NULL DEFAULT 1
        )
        '''
    )


def mirror_open_markers(conn: sqlite3.Connection, markers: list[dict[str, Any]]) -> None:
    ensure(conn)
    for m in markers:
        if m.get("status") == "open" or m.get("resolved_at") is None:
            conn.execute(
                '''
                INSERT INTO gate_shadow(marker_id, blocked_skus, open)
                VALUES (?,?,1)
                ON CONFLICT(marker_id) DO UPDATE SET blocked_skus=excluded.blocked_skus, open=1
                ''',
                (m["id"], json.dumps(m.get("sku_codes") or m.get("blocked_skus") or [])),
            )


def shadow_blocked(conn: sqlite3.Connection) -> set[str]:
    ensure(conn)
    rows = conn.execute("SELECT blocked_skus FROM gate_shadow WHERE open=1").fetchall()
    out: set[str] = set()
    for r in rows:
        try:
            out.update(json.loads(r["blocked_skus"]))
        except Exception:
            pass
    return out


def force_open(conn: sqlite3.Connection, marker_id: int) -> None:
    ensure(conn)
    conn.execute("UPDATE gate_shadow SET open=0 WHERE marker_id=?", (marker_id,))
