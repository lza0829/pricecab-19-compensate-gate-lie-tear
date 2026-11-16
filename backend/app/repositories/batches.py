from __future__ import annotations

import sqlite3
from typing import Any


def next_batch_no(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COUNT(*) c FROM apply_batches").fetchone()["c"] + 1
    return f"AB-{n:04d}"


def create_batch(
    conn: sqlite3.Connection,
    *,
    batch_no: str,
    clock_at: str,
    ticket_count: int,
    line_count: int,
    status: str,
    created_at: str,
) -> int:
    cur = conn.execute(
        '''
        INSERT INTO apply_batches(batch_no, clock_at, ticket_count, line_count, status, created_at)
        VALUES (?,?,?,?,?,?)
        ''',
        (batch_no, clock_at, ticket_count, line_count, status, created_at),
    )
    return int(cur.lastrowid)


def list_batches(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM apply_batches ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM apply_batches WHERE id=?", (batch_id,)
    ).fetchone()
    return dict(row) if row else None


def list_batch_tickets(conn: sqlite3.Connection, batch_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT t.*,
               (SELECT COUNT(*) FROM price_ticket_lines l WHERE l.ticket_id=t.id) AS line_count
        FROM price_tickets t
        WHERE t.apply_batch_id=?
        ORDER BY t.id
        ''',
        (batch_id,),
    ).fetchall()
    return [dict(r) for r in rows]
