from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect
from app.repositories import settings as settings_repo
from app.repositories import tickets as ticket_repo
from app.repositories import prices as price_repo
from app.repositories import batches as batch_repo
from app.repositories import compensation as comp_repo


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_clock() -> dict[str, Any]:
    conn = connect()
    try:
        return {
            "business_clock": settings_repo.get(conn, "business_clock"),
            "clock_label": settings_repo.get(conn, "clock_label"),
        }
    finally:
        conn.close()


def set_clock(clock_at: str) -> dict[str, Any]:
    clock_at = (clock_at or "").strip()
    if not clock_at:
        raise HTTPException(400, "业务钟不能为空")
    conn = connect(immediate=True)
    try:
        settings_repo.set_value(conn, "business_clock", clock_at)
        conn.commit()
        return get_clock()
    finally:
        conn.close()


def list_batches() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return batch_repo.list_batches(conn)
    finally:
        conn.close()


def get_batch(batch_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        batch = batch_repo.get_batch(conn, batch_id)
        if not batch:
            raise HTTPException(404, "应用批次不存在")
        batch["tickets"] = batch_repo.list_batch_tickets(conn, batch_id)
        return batch
    finally:
        conn.close()


def apply_due() -> dict[str, Any]:
    """Apply all scheduled tickets whose effective_at <= business clock."""
    conn = connect(immediate=True)
    try:
        clock = settings_repo.get(conn, "business_clock")
        if not clock:
            raise HTTPException(400, "业务钟未设置")
        due = ticket_repo.due_tickets(conn, clock)
        if not due:
            # Still a closing point: a lied marker (flipped without compensation
            # evidence) gets recomputed even when nothing is due to apply.
            reconciliation = comp_repo.reconcile_with_facts(conn, now=_now())
            conn.commit()
            return {
                "applied": False,
                "message": "当前业务钟下没有到期改价单",
                "business_clock": clock,
                "ticket_count": 0,
                "line_count": 0,
                "tickets": [],
                "resolved_marker_ids": [r["marker_id"] for r in reconciliation["resolved"]],
                "reopened_marker_ids": [r["marker_id"] for r in reconciliation["reopened"]],
                "skipped_gated": [],
            }

        # Gate (apply-time recheck): a non-compensation ticket must never land
        # while any of its SKUs is gated by a misapply marker whose applied
        # compensation does not yet cover it. The gate has no independent
        # switch: it is recomputed from compensation-ticket + price-tag-source
        # facts and snapshotted at run start, so a compensation that resolves a
        # marker in this run cannot smuggle a blocked normal ticket into the
        # same batch — the held ticket stays scheduled and is reconsidered on
        # the next run after the gate lifted. Compensation tickets are exempt
        # and processed first, which keeps the correction path free of
        # deadlocks.
        gate_snapshot = comp_repo.blocked_skus(conn)
        open_markers = comp_repo.fact_open_markers(conn)
        due.sort(key=lambda t: (0 if t["kind"] == "compensation" else 1, t["effective_at"], t["id"]))

        created = _now()
        line_total = 0
        applied_tickets = []
        skipped_gated: list[dict[str, Any]] = []
        resolved_markers: list[int] = []
        batch_id: int | None = None
        batch_no: str | None = None

        applied_count = 0
        for t in due:
            lines = ticket_repo.list_lines(conn, t["id"])
            if t["kind"] == "normal":
                overlap = sorted({l["sku_code"] for l in lines}.intersection(gate_snapshot))
                if overlap:
                    marker_nos = sorted(
                        {
                            m["marker_no"]
                            for m in open_markers
                            if set(m["sku_codes"]).intersection(overlap)
                        }
                    )
                    skipped_gated.append(
                        {
                            "ticket_no": t["ticket_no"],
                            "ticket_id": t["id"],
                            "blocked_skus": overlap,
                            "markers": marker_nos,
                        }
                    )
                    continue
            if batch_id is None:
                # create the batch lazily so an all-blocked run leaves no empty
                # batch row that would pollute the append-only history
                batch_no = batch_repo.next_batch_no(conn)
                batch_id = batch_repo.create_batch(
                    conn,
                    batch_no=batch_no,
                    clock_at=clock,
                    ticket_count=0,
                    line_count=0,
                    status="running",
                    created_at=created,
                )
            applied_count += 1
            for line in lines:
                price_repo.upsert_book(
                    conn,
                    sku_code=line["sku_code"],
                    price=float(line["new_price"]),
                    ticket_no=t["ticket_no"],
                    effective_at=t["effective_at"],
                    updated_at=created,
                )
                line_total += 1
            ticket_repo.mark_applied(conn, t["id"], applied_at=created, batch_id=batch_id)
            applied_tickets.append(t["ticket_no"])

        # Single closing point: recompute every marker from compensation facts
        # after the whole due run. Compensation writes went through the exact
        # same price-book/history path as normal tickets, so a marker resolves
        # only when applied compensation tickets cover all its SKUs with
        # price-tag-source evidence; a marker lacking that evidence is forced
        # back open even if its status was flipped without proof.
        reconciliation = comp_repo.reconcile_with_facts(conn, now=created)
        resolved_markers = [r["marker_id"] for r in reconciliation["resolved"]]

        if batch_id is None:
            # Everything due was held back by the gate. Still persist any
            # fact-based marker corrections (e.g. a lied gate re-closing).
            conn.commit()
            return {
                "applied": False,
                "message": "到期普通改价单均处于误用纠偏禁闸期，已跳过；请先应用补偿单",
                "business_clock": clock,
                "ticket_count": 0,
                "line_count": 0,
                "tickets": [],
                "resolved_marker_ids": resolved_markers,
                "reopened_marker_ids": [r["marker_id"] for r in reconciliation["reopened"]],
                "skipped_gated": skipped_gated,
            }

        conn.execute(
            "UPDATE apply_batches SET ticket_count=?, line_count=?, status=? WHERE id=?",
            (applied_count, line_total, "done", batch_id),
        )
        conn.commit()
        return {
            "applied": True,
            "batch_no": batch_no,
            "business_clock": clock,
            "ticket_count": applied_count,
            "line_count": line_total,
            "tickets": applied_tickets,
            "resolved_marker_ids": resolved_markers,
            "reopened_marker_ids": [r["marker_id"] for r in reconciliation["reopened"]],
            "skipped_gated": skipped_gated,
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
