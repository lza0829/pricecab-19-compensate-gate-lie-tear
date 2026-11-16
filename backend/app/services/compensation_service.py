"""Forward-only compensation workflow.

Two mutually exclusive strategies (one global setting), chosen per marker at
marking time:

* ``auto_draft`` — marking an applied ticket/batch generates a compensation
  ticket *draft* (reverse/target prices). It still has to mature and go through
  the normal due-application before anything is written to the price book.
  Until that draft is applied, the flagged SKUs are gated against any other
  non-compensation ticket.
* ``manual`` — no draft is created. The flagged SKUs stay gated until a human
  builds a compensation ticket pointing at the marker and it gets applied.

Successful batches, price-book writes and history rows are immutable; this
module only appends new markers and new compensation tickets.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect
from app.repositories import batches as batch_repo
from app.repositories import compensation as comp_repo
from app.repositories import gate_shadow
from app.repositories import settings as settings_repo
from app.repositories import tickets as ticket_repo

STRATEGY_KEY = "compensation_strategy"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Global, mutually exclusive strategy
# ---------------------------------------------------------------------------

def get_strategy() -> dict[str, str]:
    conn = connect()
    try:
        value = settings_repo.get(conn, STRATEGY_KEY, comp_repo.STRATEGY_MANUAL)
        return {
            "compensation_strategy": value,
            "options": [comp_repo.STRATEGY_AUTO_DRAFT, comp_repo.STRATEGY_MANUAL],
        }
    finally:
        conn.close()


def set_strategy(strategy: str) -> dict[str, str]:
    if strategy not in comp_repo.STRATEGIES:
        raise HTTPException(400, "策略只能是 auto_draft 或 manual（全局互斥，二选一）")
    conn = connect(immediate=True)
    try:
        settings_repo.set_value(conn, STRATEGY_KEY, strategy)
        conn.commit()
        return get_strategy()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Markers & gates
# ---------------------------------------------------------------------------

def list_markers() -> list[dict[str, Any]]:
    conn = connect()
    try:
        markers = comp_repo.list_markers(conn)
        for m in markers:
            m["compensation_tickets"] = _marker_tickets(conn, m["id"])
        return markers
    finally:
        conn.close()


def get_marker(marker_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        marker = comp_repo.get_marker(conn, marker_id)
        if not marker:
            raise HTTPException(404, "误用标记不存在")
        marker["compensation_tickets"] = _marker_tickets(conn, marker_id)
        return marker
    finally:
        conn.close()


def _marker_tickets(conn, marker_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT t.id, t.ticket_no, t.title, t.status, t.effective_at,
               t.applied_at, t.apply_batch_id, t.kind, t.compensation_mode
        FROM price_tickets t
        WHERE t.compensates_marker_id=?
        ORDER BY t.id
        ''',
        (marker_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_gates() -> dict[str, Any]:
    conn = connect()
    try:
        gates = comp_repo.list_gates(conn)
        markers = comp_repo.open_markers(conn)
        gate_shadow.mirror_open_markers(conn, markers)
        shadow = gate_shadow.shadow_blocked(conn)
        blocked = sorted(set(shadow) | {s for g in gates for s in g["blocked_skus"]})
        return {"blocked_skus": blocked, "gates": gates}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Mark a misapplied ticket / batch
# ---------------------------------------------------------------------------

def mark_misapply(body: dict[str, Any]) -> dict[str, Any]:
    target_type = (body.get("target_type") or "").strip()
    target_id = body.get("target_id")
    reason = (body.get("reason") or "").strip() or None
    if target_type not in ("ticket", "batch"):
        raise HTTPException(400, "target_type 只能是 ticket 或 batch")
    if not isinstance(target_id, int):
        raise HTTPException(400, "target_id 必填且为整数")

    conn = connect(immediate=True)
    try:
        strategy = settings_repo.get(conn, STRATEGY_KEY, comp_repo.STRATEGY_MANUAL)
        sku_codes, target_ticket_id, target_batch_id = _resolve_target(
            conn, target_type, target_id
        )
        _ensure_no_open_duplicate(conn, target_type, target_ticket_id, target_batch_id)

        marker_id = comp_repo.create_marker(
            conn,
            marker_no=comp_repo.next_marker_no(conn),
            target_type=target_type,
            target_ticket_id=target_ticket_id,
            target_batch_id=target_batch_id,
            sku_codes=sku_codes,
            reason=reason,
            strategy=strategy,
            created_at=_now(),
        )

        draft = None
        if strategy == comp_repo.STRATEGY_AUTO_DRAFT:
            draft = _create_draft(conn, marker_id, sku_codes, target_type, target_id)

        conn.commit()
        marker = comp_repo.get_marker(conn, marker_id)
        marker["compensation_tickets"] = _marker_tickets(conn, marker_id)
        return {"marker": marker, "draft_ticket": draft}
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _resolve_target(conn, target_type: str, target_id: int) -> tuple[list[str], int | None, int | None]:
    if target_type == "ticket":
        ticket = ticket_repo.get_ticket(conn, target_id)
        if not ticket:
            raise HTTPException(404, "改价单不存在")
        if ticket["status"] != "applied":
            raise HTTPException(400, "只能标记已应用（成功批次）的改价单为误用；未应用单可直接弃用，无需补偿")
        lines = ticket_repo.list_lines(conn, target_id)
        skus = sorted({l["sku_code"] for l in lines})
        return skus, target_id, None

    batch = batch_repo.get_batch(conn, target_id)
    if not batch:
        raise HTTPException(404, "应用批次不存在")
    tickets = batch_repo.list_batch_tickets(conn, target_id)
    if not tickets:
        raise HTTPException(400, "该批次没有任何改价单，无法标记误用")
    sku_set: set[str] = set()
    for t in tickets:
        for l in ticket_repo.list_lines(conn, t["id"]):
            sku_set.add(l["sku_code"])
    return sorted(sku_set), None, target_id


def _ensure_no_open_duplicate(
    conn, target_type: str, target_ticket_id: int | None, target_batch_id: int | None
) -> None:
    if target_type == "ticket":
        row = conn.execute(
            '''
            SELECT marker_no FROM misapply_markers
            WHERE status='open' AND target_type='ticket' AND target_ticket_id=?
            ''',
            (target_ticket_id,),
        ).fetchone()
    else:
        row = conn.execute(
            '''
            SELECT marker_no FROM misapply_markers
            WHERE status='open' AND target_type='batch' AND target_batch_id=?
            ''',
            (target_batch_id,),
        ).fetchone()
    if row:
        raise HTTPException(409, f"该目标已存在未解除的误用标记 {row['marker_no']}")


def _reverse_targets(
    conn, target_type: str, target_id: int, sku_codes: list[str]
) -> dict[str, float]:
    """Compensation target price = price in force immediately before the misuse.

    Ticket target: the ticket line's own old_price.
    Batch target: per SKU, the old_price of the *first* ticket in the batch
    that touched it (the state before the batch's first write). Later tickets
    only carry intermediate within-batch prices and must not overwrite it.
    """
    targets: dict[str, float] = {}
    if target_type == "ticket":
        for line in ticket_repo.list_lines(conn, target_id):
            targets[line["sku_code"]] = float(line["old_price"])
    else:
        for t in batch_repo.list_batch_tickets(conn, target_id):
            for line in ticket_repo.list_lines(conn, t["id"]):
                # list order is ticket id ascending; first touch wins
                targets.setdefault(line["sku_code"], float(line["old_price"]))
    missing = [s for s in sku_codes if s not in targets]
    if missing:
        raise HTTPException(500, f"无法推导纠偏目标价：{','.join(missing)}")
    return targets


def _create_draft(
    conn, marker_id: int, sku_codes: list[str], target_type: str, target_id: int
) -> dict[str, Any]:
    if comp_repo.has_draft_for_marker(conn, marker_id):
        raise HTTPException(409, "该标记已生成过补偿草稿，不能重复生成")
    marker = comp_repo.get_marker(conn, marker_id)
    clock = settings_repo.get(conn, "business_clock")
    if not clock:
        raise HTTPException(400, "业务钟未设置，无法生成补偿草稿")
    # Effective at >= current business clock; using the clock itself matures
    # on the next "apply due" without ever back-dating.
    targets = _reverse_targets(conn, target_type, target_id, sku_codes)
    return _create_compensation_ticket(
        conn,
        marker_id=marker_id,
        effective_at=clock,
        lines=[{"sku_code": s, "new_price": targets[s]} for s in sku_codes],
        note=f"自动草稿：纠偏 {marker['marker_no']}（反向恢复到误用前价签）",
        mode="auto_draft",
    )


# ---------------------------------------------------------------------------
# Manual compensation tickets
# ---------------------------------------------------------------------------

def create_compensation_ticket(body: dict[str, Any]) -> dict[str, Any]:
    marker_id = body.get("marker_id")
    effective_at = (body.get("effective_at") or "").strip()
    lines = body.get("lines") or []
    note = (body.get("note") or "").strip() or None
    if not isinstance(marker_id, int):
        raise HTTPException(400, "marker_id 必填且为整数")
    if not effective_at:
        raise HTTPException(400, "生效时刻必填（须 ≥ 当前业务钟）")
    if not lines:
        raise HTTPException(400, "至少一行补偿明细")

    conn = connect(immediate=True)
    try:
        marker = comp_repo.get_marker(conn, marker_id)
        if not marker:
            raise HTTPException(404, "误用标记不存在")
        if marker["status"] != comp_repo.MARKER_OPEN:
            raise HTTPException(409, "该误用标记已解除，无需再建补偿单")
        if marker["strategy"] != comp_repo.STRATEGY_MANUAL:
            raise HTTPException(409, "当前标记采用自动草稿策略，补偿单由系统生成，不接受人工建单")
        clock = settings_repo.get(conn, "business_clock")
        if not clock:
            raise HTTPException(400, "业务钟未设置")
        if effective_at < clock:
            raise HTTPException(400, f"补偿单生效时刻 {effective_at} 早于当前业务钟 {clock}，禁止回溯生效")

        seen: set[str] = set()
        clean_lines = []
        allowed = set(marker["sku_codes"])
        for line in lines:
            sku_code = (line.get("sku_code") or "").strip()
            new_price = line.get("new_price")
            if not sku_code:
                raise HTTPException(400, "补偿明细缺少 sku_code")
            if sku_code not in allowed:
                raise HTTPException(400, f"{sku_code} 不在标记 {marker['marker_no']} 的禁闸范围内")
            if sku_code in seen:
                raise HTTPException(400, f"{sku_code} 在同一张补偿单中重复")
            if new_price is None:
                raise HTTPException(400, f"{sku_code} 缺少纠偏目标价 new_price")
            seen.add(sku_code)
            clean_lines.append({"sku_code": sku_code, "new_price": float(new_price)})

        ticket = _create_compensation_ticket(
            conn,
            marker_id=marker_id,
            effective_at=effective_at,
            lines=clean_lines,
            note=note,
            mode="manual",
        )
        conn.commit()
        return ticket
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _create_compensation_ticket(
    conn,
    *,
    marker_id: int,
    effective_at: str,
    lines: list[dict[str, Any]],
    note: str | None,
    mode: str,
) -> dict[str, Any]:
    marker = comp_repo.get_marker(conn, marker_id)
    ticket_no = ticket_repo.next_ticket_no(conn)
    tid = ticket_repo.create_ticket(
        conn,
        ticket_no=ticket_no,
        title=f"补偿单（{marker['marker_no']}）",
        effective_at=effective_at,
        note=note,
        created_at=_now(),
        kind="compensation",
        compensates_marker_id=marker_id,
        compensation_mode=mode,
    )
    for line in lines:
        book = conn.execute(
            "SELECT current_price FROM price_book WHERE sku_code=?",
            (line["sku_code"],),
        ).fetchone()
        old_price = float(book["current_price"]) if book else 0.0
        ticket_repo.add_line(
            conn,
            ticket_id=tid,
            sku_code=line["sku_code"],
            old_price=old_price,
            new_price=float(line["new_price"]),
        )
    return ticket_repo.get_ticket(conn, tid) | {
        "lines": ticket_repo.list_lines(conn, tid)
    }
