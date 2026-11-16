"""Misapply markers, compensation tickets and the derived apply-gate.

Forward-only correction model:
- A successful apply (price_book / price_history / apply_batches row) is never
  deleted or rewritten.
- A misapply is *marked*; the marker puts the affected SKUs behind an apply-gate.
- The gate is NOT an independent status table and there is no switch that can be
  toggled on its own. It is recomputed from facts only:
    1. applied compensation tickets pointing at the marker, and
    2. the price_history rows (the price-tag source evidence) those tickets wrote.
  The gate is lifted only after applied compensation tickets point at the marker
  and cover every SKU it flagged, with an applied price-tag source row for each.

Single global closing rule (the two possible closings are mutually exclusive;
this codebase implements the fact-first one):
  * no applied compensation covering a marker  -> marker forced back open, gate
    stays closed, normal applies are rejected;
  * applied compensation fully covers it       -> marker resolved, gate lifted.
The marker.status column is only a cached projection of those facts and is
recomputed by ``reconcile_with_facts``; it never decides the gate by itself.
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


def has_draft_for_marker(conn: sqlite3.Connection, marker_id: int) -> bool:
    """auto_draft strategy may only produce one draft per marker."""
    row = conn.execute(
        "SELECT COUNT(*) c FROM price_tickets WHERE compensates_marker_id=?",
        (marker_id,),
    ).fetchone()
    return int(row["c"]) > 0


# ---------------------------------------------------------------------------
# Apply-gate: derived purely from compensation *facts*. There is no separate
# gate/open boolean anywhere — a marker without fully-covered applied
# compensation is open regardless of what its status column says.
# ---------------------------------------------------------------------------

def applied_compensation_skus(conn: sqlite3.Connection, marker_id: int) -> set[str]:
    """SKUs that have an *applied* compensation ticket for this marker AND an
    applied price-tag source row (price_history) written by that ticket."""
    rows = conn.execute(
        '''
        SELECT DISTINCT l.sku_code AS sku_code
        FROM price_tickets t
        JOIN price_ticket_lines l ON l.ticket_id = t.id
        WHERE t.compensates_marker_id=? AND t.status='applied'
          AND EXISTS (
            SELECT 1 FROM price_history h
            WHERE h.sku_code = l.sku_code
              AND h.source_ticket_no = t.ticket_no
          )
        ''',
        (marker_id,),
    ).fetchall()
    return {r["sku_code"] for r in rows}


def is_covered(conn: sqlite3.Connection, marker: dict[str, Any]) -> bool:
    covered = applied_compensation_skus(conn, marker["id"])
    return set(marker["sku_codes"]).issubset(covered)


def fact_open_markers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Markers that must gate their SKUs *right now*, per compensation facts.

    Every marker not fully covered by applied compensation is open — even if its
    status column was flipped to 'resolved' without evidence (a lied gate)."""
    return [m for m in list_markers(conn, limit=10_000) if not is_covered(conn, m)]


def blocked_skus(conn: sqlite3.Connection) -> set[str]:
    blocked: set[str] = set()
    for m in fact_open_markers(conn):
        blocked.update(m["sku_codes"])
    return blocked


def blocked_for_skus(conn: sqlite3.Connection, sku_codes: list[str]) -> list[dict[str, Any]]:
    """Return fact-open markers that gate any of the given SKUs."""
    wanted = set(sku_codes)
    hits = []
    for m in fact_open_markers(conn):
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
    for m in fact_open_markers(conn):
        covered = applied_compensation_skus(conn, m["id"])
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
                "covered_skus": sorted(covered.intersection(m["sku_codes"])),
                "missing_skus": sorted(set(m["sku_codes"]) - covered),
            }
        )
    return gates


# ---------------------------------------------------------------------------
# Recompute the marker projection from facts. This is the single closing point
# for a lied gate: both directions are corrected here, and it is the only place
# allowed to change a marker's open/resolved state.
# ---------------------------------------------------------------------------

def reconcile_with_facts(
    conn: sqlite3.Connection, *, now: str
) -> dict[str, list[dict[str, Any]]]:
    """Recompute every marker's status from applied-compensation facts.

    * covered (all SKUs have applied compensation + price-tag source evidence):
      status='resolved', resolved_at/resolved_ticket_id filled from the latest
      applied compensation ticket;
    * not covered: status forced back to 'open' and any stale resolution
      pointers are cleared, so the gate re-closes and normal applies are
      refused until a compensation ticket is built and applied.
    """
    reopened: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    for m in list_markers(conn, limit=10_000):
        covered = applied_compensation_skus(conn, m["id"])
        if set(m["sku_codes"]).issubset(covered):
            latest = conn.execute(
                '''
                SELECT id, applied_at FROM price_tickets
                WHERE compensates_marker_id=? AND status='applied'
                ORDER BY applied_at DESC, id DESC LIMIT 1
                ''',
                (m["id"],),
            ).fetchone()
            resolved_at = latest["applied_at"] if latest and latest["applied_at"] else now
            resolved_ticket_id = latest["id"] if latest else None
            if (
                m["status"] != MARKER_RESOLVED
                or m["resolved_ticket_id"] != resolved_ticket_id
            ):
                conn.execute(
                    '''
                    UPDATE misapply_markers
                    SET status=?, resolved_at=?, resolved_ticket_id=?
                    WHERE id=?
                    ''',
                    (MARKER_RESOLVED, resolved_at, resolved_ticket_id, m["id"]),
                )
                resolved.append(
                    {
                        "marker_id": m["id"],
                        "marker_no": m["marker_no"],
                        "resolved_ticket_id": resolved_ticket_id,
                    }
                )
        else:
            if m["status"] != MARKER_OPEN or m["resolved_at"] is not None:
                conn.execute(
                    '''
                    UPDATE misapply_markers
                    SET status=?, resolved_at=NULL, resolved_ticket_id=NULL
                    WHERE id=?
                    ''',
                    (MARKER_OPEN, m["id"]),
                )
                reopened.append(
                    {
                        "marker_id": m["id"],
                        "marker_no": m["marker_no"],
                        "missing_skus": sorted(set(m["sku_codes"]) - covered),
                    }
                )
    return {"reopened": reopened, "resolved": resolved}


def integrity_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """Cross-check the three stores that must agree: marker table, compensation
    tickets, price-tag source (price_book / price_history). Read-only."""
    divergences: list[dict[str, Any]] = []
    for m in list_markers(conn, limit=10_000):
        covered = applied_compensation_skus(conn, m["id"])
        missing = sorted(set(m["sku_codes"]) - covered)
        fact_open = bool(missing)

        # marker table vs facts
        if m["status"] == MARKER_RESOLVED and fact_open:
            divergences.append(
                {
                    "kind": "resolved_without_compensation",
                    "marker_id": m["id"],
                    "marker_no": m["marker_no"],
                    "missing_skus": missing,
                }
            )
        if m["status"] == MARKER_OPEN and not fact_open:
            divergences.append(
                {
                    "kind": "compensated_but_still_gated",
                    "marker_id": m["id"],
                    "marker_no": m["marker_no"],
                    "blocked_skus": m["sku_codes"],
                }
            )

        # applied compensation ticket without a matching price-tag source row:
        # the application entry and the price-tag source disagree.
        rows = conn.execute(
            '''
            SELECT DISTINCT t.ticket_no AS ticket_no, l.sku_code AS sku_code
            FROM price_tickets t
            JOIN price_ticket_lines l ON l.ticket_id = t.id
            WHERE t.compensates_marker_id=? AND t.status='applied'
            ''',
            (m["id"],),
        ).fetchall()
        for r in rows:
            history_hit = conn.execute(
                '''
                SELECT 1 FROM price_history
                WHERE sku_code=? AND source_ticket_no=? LIMIT 1
                ''',
                (r["sku_code"], r["ticket_no"]),
            ).fetchone()
            if history_hit is None:
                divergences.append(
                    {
                        "kind": "applied_compensation_without_price_source",
                        "marker_id": m["id"],
                        "marker_no": m["marker_no"],
                        "ticket_no": r["ticket_no"],
                        "sku_code": r["sku_code"],
                    }
                )

        # A normal-ticket price tag whose source ticket was applied *after* the
        # marker was created proves the gate was bypassed while closed. The
        # misuse ticket's own tag (applied before marking) is legitimate
        # forward-only history and must not be flagged.
        if fact_open:
            for sku in m["sku_codes"]:
                book = conn.execute(
                    "SELECT source_ticket_no FROM price_book WHERE sku_code=?",
                    (sku,),
                ).fetchone()
                if book is not None and book["source_ticket_no"] is not None:
                    src = conn.execute(
                        '''
                        SELECT kind, status, applied_at FROM price_tickets
                        WHERE ticket_no=?
                        ''',
                        (book["source_ticket_no"],),
                    ).fetchone()
                    if (
                        src is not None
                        and src["kind"] == "normal"
                        and src["applied_at"] is not None
                        and src["applied_at"] >= m["created_at"]
                    ):
                        divergences.append(
                            {
                                "kind": "gated_sku_price_tag_from_normal_ticket",
                                "marker_id": m["id"],
                                "marker_no": m["marker_no"],
                                "sku_code": sku,
                                "source_ticket_no": book["source_ticket_no"],
                            }
                        )

    shadow_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gate_shadow'"
    ).fetchone()
    return {
        "divergent": bool(divergences),
        "divergences": divergences,
        "blocked_skus": sorted(blocked_skus(conn)),
        "gate_shadow_table_present": shadow_exists is not None,
    }
