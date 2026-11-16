"""Misapply markers, compensation tickets and the derived apply-gate.

Forward-only correction model:
- A successful apply (price_book / price_history / apply_batches row) is never
  deleted or rewritten.
- A misapply is *marked*; the marker puts the affected SKUs behind an apply-gate.
- The gate is lifted only after an applied compensation ticket points at the
  marker and covers every SKU it flagged.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

STRATEGY_AUTO_DRAFT = "auto_draft"
STRATEGY_MANUAL = "manual"
STRATEGIES = (STRATEGY_AUTO_DRAFT, STRATEGY_MANUAL)

MARKER_OPEN = "open"
MARKER_RESOLVED = "resolved"


def next_marker_no(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COUNT(*) c FROM misapply_markers").fetchone()["c"] + 1
    return f"CM-{n:04d}"


def create_marker(
    conn: sqlite3.Connection,
    *,
    marker_no: str,
    target_type: str,
    target_ticket_id: int | None,
    target_batch_id: int | None,
    sku_codes: list[str],
    reason: str | None,
    strategy: str,
    created_at: str,
) -> int:
    cur = conn.execute(
        '''
        INSERT INTO misapply_markers(
          marker_no, target_type, target_ticket_id, target_batch_id,
          sku_codes, reason, strategy, status, created_at
        )
        VALUES (?,?,?,?,?,?,?,?,?)
        ''',
        (
            marker_no,
            target_type,
            target_ticket_id,
            target_batch_id,
            json.dumps(sku_codes, ensure_ascii=False),
            reason,
            strategy,
            MARKER_OPEN,
            created_at,
        ),
    )
    return int(cur.lastrowid)


def get_marker(conn: sqlite3.Connection, marker_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM misapply_markers WHERE id=?", (marker_id,)
    ).fetchone()
    return _hydrate(row)


def list_markers(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM misapply_markers ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_hydrate(r) for r in rows]


def _hydrate(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    d["sku_codes"] = json.loads(d["sku_codes"])
    return d


def open_markers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM misapply_markers WHERE status=? ORDER BY id",
        (MARKER_OPEN,),
    ).fetchall()
    return [_hydrate(r) for r in rows]


def has_draft_for_marker(conn: sqlite3.Connection, marker_id: int) -> bool:
    """auto_draft strategy may only produce one draft per marker."""
    row = conn.execute(
        "SELECT COUNT(*) c FROM price_tickets WHERE compensates_marker_id=?",
        (marker_id,),
    ).fetchone()
    return int(row["c"]) > 0


# ---------------------------------------------------------------------------
# Apply-gate: derived purely from open markers, so gate / book / history /
# reconciliation always share the same source of truth.
# ---------------------------------------------------------------------------

def blocked_skus(conn: sqlite3.Connection) -> set[str]:
    blocked: set[str] = set()
    for m in open_markers(conn):
        blocked.update(m["sku_codes"])
    return blocked


def blocked_for_skus(conn: sqlite3.Connection, sku_codes: list[str]) -> list[dict[str, Any]]:
    """Return open markers that gate any of the given SKUs."""
    wanted = set(sku_codes)
    hits = []
    for m in open_markers(conn):
        overlap = sorted(wanted.intersection(m["sku_codes"]))
        if overlap:
            hits.append(
                {
                    "marker_id": m["id"],
                    "marker_no": m["marker_no"],
                    "strategy": m["strategy"],
                    "blocked_skus": overlap,
                }
            )
    return hits


def list_gates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    gates = []
    for m in open_markers(conn):
        gates.append(
            {
                "marker_id": m["id"],
                "marker_no": m["marker_no"],
                "target_type": m["target_type"],
                "target_ticket_id": m["target_ticket_id"],
                "target_batch_id": m["target_batch_id"],
                "strategy": m["strategy"],
                "reason": m["reason"],
                "created_at": m["created_at"],
                "blocked_skus": m["sku_codes"],
            }
        )
    return gates


def resolve_if_covered(
    conn: sqlite3.Connection, marker_id: int, *, applied_ticket_id: int, at: str
) -> bool:
    """Resolve the marker once applied compensation tickets cover all its SKUs."""
    marker = get_marker(conn, marker_id)
    if not marker or marker["status"] != MARKER_OPEN:
        return False
    rows = conn.execute(
        '''
        SELECT DISTINCT l.sku_code AS sku_code
        FROM price_tickets t
        JOIN price_ticket_lines l ON l.ticket_id = t.id
        WHERE t.compensates_marker_id=? AND t.status='applied'
        ''',
        (marker_id,),
    ).fetchall()
    covered = {r["sku_code"] for r in rows}
    if set(marker["sku_codes"]).issubset(covered):
        conn.execute(
            '''
            UPDATE misapply_markers
            SET status=?, resolved_at=?, resolved_ticket_id=?
            WHERE id=?
            ''',
            (MARKER_RESOLVED, at, applied_ticket_id, marker_id),
        )
        return True
    return False
