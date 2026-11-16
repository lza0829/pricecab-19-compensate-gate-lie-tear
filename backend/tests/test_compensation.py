"""End-to-end tests for the forward-only compensation workflow.

Run with:  cd backend && ../../tmp/venv/bin/python -m pytest -q
(or any interpreter that has fastapi + httpx installed)
"""
from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.TemporaryDirectory()
    monkeypatch.setenv("DATA_DIR", tmp.name)
    # Import after DATA_DIR is set; reload config so DB_PATH points at the
    # temp dir (app.db reads config.DB_PATH lazily on every connect).
    import importlib

    import app.config as config
    importlib.reload(config)
    import app.main as main
    importlib.reload(main)
    main.seed_if_empty()
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c
    tmp.cleanup()


SKU = "SKU-TEA-001"  # list price 28.0


def _set_clock(client, at: str):
    r = client.put("/api/apply/clock", json={"business_clock": at})
    assert r.status_code == 200, r.text


def _make_ticket(client, effective_at, sku=SKU, price=99.0, title="普通改价单"):
    r = client.post(
        "/api/tickets",
        json={
            "title": title,
            "effective_at": effective_at,
            "lines": [{"sku_code": sku, "new_price": price}],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _apply_due(client, expected=200):
    r = client.post("/api/apply/due")
    assert r.status_code == expected, r.text
    return r.json() if expected == 200 else r.json()


def _book_price(client, sku=SKU):
    rows = client.get("/api/prices/book").json()
    return next(r["current_price"] for r in rows if r["sku_code"] == sku)


# ---------------------------------------------------------------------------
# Strategy is global & mutually exclusive
# ---------------------------------------------------------------------------

def test_strategy_default_and_validation(client):
    s = client.get("/api/compensation/strategy").json()
    assert s["compensation_strategy"] == "manual"
    assert set(s["options"]) == {"auto_draft", "manual"}

    r = client.put(
        "/api/compensation/strategy", json={"compensation_strategy": "auto_draft"}
    )
    assert r.status_code == 200
    assert r.json()["compensation_strategy"] == "auto_draft"

    r = client.put(
        "/api/compensation/strategy", json={"compensation_strategy": "nonsense"}
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Manual strategy: mark -> gate blocks -> manual comp -> applied -> gate lifts
# ---------------------------------------------------------------------------

def test_manual_strategy_full_flow(client):
    _set_clock(client, "2026-11-16T09:00:00Z")

    # 1. A normal ticket gets applied successfully (the misapplied batch).
    bad = _make_ticket(client, "2026-11-16T09:30:00Z", price=99.0, title="误应用单")
    _set_clock(client, "2026-11-16T10:00:00Z")
    res = _apply_due(client)
    assert res["applied"] is True
    assert _book_price(client) == 99.0
    ticket_id = bad["id"]
    history_before = client.get("/api/prices/history", params={"sku_code": SKU}).json()
    assert any(h["source_ticket_no"] == bad["ticket_no"] and h["price"] == 99.0 for h in history_before)

    # Only applied tickets may be marked.
    future = _make_ticket(client, "2026-11-20T00:00:00Z", price=50.0)
    r = client.post(
        "/api/compensation/markers",
        json={"target_type": "ticket", "target_id": future["id"]},
    )
    assert r.status_code == 400

    # 2. Mark the misapplied ticket. A normal ticket scheduled beforehand will
    #    later exercise the apply-time recheck (it could not be caught at build).
    blocked_normal = _make_ticket(client, "2026-11-16T10:30:00Z", price=77.0)
    r = client.post(
        "/api/compensation/markers",
        json={"target_type": "ticket", "target_id": ticket_id, "reason": "价格填错"},
    )
    assert r.status_code == 200, r.text
    marker = r.json()["marker"]
    assert marker["status"] == "open"
    assert marker["strategy"] == "manual"
    assert SKU in marker["sku_codes"]
    assert r.json()["draft_ticket"] is None  # manual => no system draft

    # duplicate open marker rejected
    r = client.post(
        "/api/compensation/markers",
        json={"target_type": "ticket", "target_id": ticket_id},
    )
    assert r.status_code == 409

    # 3. Gate is visible and blocks new non-comp tickets at build time.
    gates = client.get("/api/compensation/gates").json()
    assert SKU in gates["blocked_skus"]
    r = client.post(
        "/api/tickets",
        json={
            "title": "禁闸期普通单",
            "effective_at": "2026-11-16T11:00:00Z",
            "lines": [{"sku_code": SKU, "new_price": 88.0}],
        },
    )
    assert r.status_code == 409, r.text
    # unrelated SKU is not blocked
    other = _make_ticket(client, "2026-11-16T11:00:00Z", sku="SKU-TEA-002", price=70.0)
    assert other["id"]

    # 4. Manual compensation ticket: effective time must not pre-date the clock.
    r = client.post(
        "/api/compensation/tickets",
        json={
            "marker_id": marker["id"],
            "effective_at": "2026-11-16T09:59:00Z",
            "lines": [{"sku_code": SKU, "new_price": 28.0}],
        },
    )
    assert r.status_code == 400
    # SKUs outside the marker are rejected.
    r = client.post(
        "/api/compensation/tickets",
        json={
            "marker_id": marker["id"],
            "effective_at": "2026-11-16T12:00:00Z",
            "lines": [{"sku_code": "SKU-TEA-002", "new_price": 28.0}],
        },
    )
    assert r.status_code == 400

    r = client.post(
        "/api/compensation/tickets",
        json={
            "marker_id": marker["id"],
            "effective_at": "2026-11-16T12:00:00Z",
            "note": "人工纠偏回 28",
            "lines": [{"sku_code": SKU, "new_price": 28.0}],
        },
    )
    assert r.status_code == 200, r.text
    comp = r.json()
    assert comp["kind"] == "compensation"
    assert comp["compensates_marker_id"] == marker["id"]
    assert comp["compensation_mode"] == "manual"
    assert comp["status"] == "scheduled"

    # 5. While the gate is closed, a normal ticket becoming due is held back:
    #    it is reported as skipped, stays scheduled, and no batch includes it.
    #    Other SKUs' due tickets still apply normally in the same run.
    _set_clock(client, "2026-11-16T11:00:00Z")  # blocked normal (10:30) due, comp (12:00) not yet
    res = _apply_due(client)
    assert [s["ticket_id"] for s in res["skipped_gated"]] == [blocked_normal["id"]]
    assert blocked_normal["ticket_no"] not in res["tickets"]
    assert _book_price(client) == 99.0
    assert client.get(f"/api/tickets/{blocked_normal['id']}").json()["status"] == "scheduled"
    assert client.get(f"/api/tickets/{other['id']}").json()["status"] == "applied"
    batches_before = len(client.get("/api/apply/batches").json())

    # 6. The compensation matures; it applies through the same due path, writes
    #    book/history and resolves the marker. The blocked normal ticket is due
    #    too but stays held for the whole run (gate snapshot at run start).
    _set_clock(client, "2026-11-16T12:30:00Z")
    res = _apply_due(client)
    assert res["applied"] is True
    assert [s["ticket_id"] for s in res["skipped_gated"]] == [blocked_normal["id"]]
    assert res["skipped_gated"][0]["markers"] == [marker["marker_no"]]
    assert comp["ticket_no"] in res["tickets"]
    assert marker["id"] in res["resolved_marker_ids"]
    assert _book_price(client) == 28.0
    assert len(client.get("/api/apply/batches").json()) == batches_before + 1
    assert client.get(f"/api/tickets/{blocked_normal['id']}").json()["status"] == "scheduled"

    # gate lifted; marker resolved
    marker2 = client.get(f"/api/compensation/markers/{marker['id']}").json()
    assert marker2["status"] == "resolved"
    assert marker2["resolved_at"]
    assert client.get("/api/compensation/gates").json()["blocked_skus"] == []

    # 7. Once lifted, the held-back normal ticket applies on the next run.
    _apply_due(client)
    assert client.get(f"/api/tickets/{blocked_normal['id']}").json()["status"] == "applied"
    assert _book_price(client) == 77.0

    # gate lifted; marker resolved
    marker2 = client.get(f"/api/compensation/markers/{marker['id']}").json()
    assert marker2["status"] == "resolved"
    assert marker2["resolved_at"]
    assert client.get("/api/compensation/gates").json()["blocked_skus"] == []

    # 8. Forward-only: the misapplied history row and its batch still exist.
    history_after = client.get("/api/prices/history", params={"sku_code": SKU}).json()
    prices_in_order = [(h["source_ticket_no"], h["price"]) for h in reversed(history_after)]
    assert (bad["ticket_no"], 99.0) in prices_in_order
    assert (comp["ticket_no"], 28.0) in prices_in_order
    assert (blocked_normal["ticket_no"], 77.0) in prices_in_order
    batches = client.get("/api/apply/batches").json()
    assert len(batches) == 4  # nothing was deleted or rewritten
    first_batch = client.get(f"/api/apply/batches/{batches[-1]['id']}").json()
    assert any(t["id"] == ticket_id for t in first_batch["tickets"])

    # 9. Once lifted, new normal tickets apply again.
    again = _make_ticket(client, "2026-11-16T13:00:00Z", price=31.5)
    _set_clock(client, "2026-11-16T13:30:00Z")
    _apply_due(client)
    assert _book_price(client) == 31.5
    assert client.get(f"/api/tickets/{again['id']}").json()["status"] == "applied"

    # 10. Reconciliation still reads the same append-only history (no holes).
    rec = client.get("/api/reconcile/sales").json()
    tea_sale = next(r for r in rec["rows"] if r["sku_code"] == SKU)
    # sold at 08:10 before any ticket: as-of price must remain the list price
    assert tea_sale["expected_price"] == 28.0


# ---------------------------------------------------------------------------
# Auto-draft strategy: mark -> system draft -> same due rules -> apply resolves
# ---------------------------------------------------------------------------

def test_auto_draft_strategy_full_flow(client):
    client.put("/api/compensation/strategy", json={"compensation_strategy": "auto_draft"})
    _set_clock(client, "2026-11-16T09:00:00Z")

    # misapplied batch: two SKUs in one ticket
    bad = _make_ticket(client, "2026-11-16T09:30:00Z", price=120.0, title="误用单")
    r = client.post(
        "/api/tickets",
        json={
            "title": "误用单-第二SKU",
            "effective_at": "2026-11-16T09:30:00Z",
            "lines": [{"sku_code": "SKU-SNK-011", "new_price": 55.0}],
        },
    )
    assert r.status_code == 200
    _set_clock(client, "2026-11-16T10:00:00Z")
    _apply_due(client)
    assert _book_price(client) == 120.0

    # mark by batch id
    batch = client.get("/api/apply/batches").json()[0]
    r = client.post(
        "/api/compensation/markers",
        json={"target_type": "batch", "target_id": batch["id"], "reason": "整批误用"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    marker = payload["marker"]
    draft = payload["draft_ticket"]
    assert marker["strategy"] == "auto_draft"
    assert set(marker["sku_codes"]) == {SKU, "SKU-SNK-011"}
    # draft contains reverse targets (pre-misuse prices) and is still scheduled
    assert draft is not None
    assert draft["kind"] == "compensation"
    assert draft["compensation_mode"] == "auto_draft"
    assert draft["status"] == "scheduled"
    target_prices = {l["sku_code"]: l["new_price"] for l in draft["lines"]}
    assert target_prices == {SKU: 28.0, "SKU-SNK-011": 12.5}
    # forward-only rule: effective at >= current business clock
    assert draft["effective_at"] >= "2026-11-16T10:00:00Z"

    # manual comp creation refused under auto-draft strategy
    r = client.post(
        "/api/compensation/tickets",
        json={
            "marker_id": marker["id"],
            "effective_at": "2026-11-16T11:00:00Z",
            "lines": [{"sku_code": SKU, "new_price": 28.0}],
        },
    )
    assert r.status_code == 409

    # gated: build-time rejection for both flagged SKUs, but not other SKUs
    for sku in (SKU, "SKU-SNK-011"):
        r = client.post(
            "/api/tickets",
            json={
                "title": "禁闸期普通单",
                "effective_at": "2026-11-16T10:30:00Z",
                "lines": [{"sku_code": sku, "new_price": 1.0}],
            },
        )
        assert r.status_code == 409
    ok = _make_ticket(client, "2026-11-16T10:30:00Z", sku="SKU-DAY-021", price=40.0)
    assert ok["id"]

    # applying the matured draft writes the price book via the same due path
    _set_clock(client, "2026-11-16T11:00:00Z")
    res = _apply_due(client)
    assert res["applied"] is True
    assert marker["id"] in res["resolved_marker_ids"]
    assert _book_price(client) == 28.0
    rows = client.get("/api/prices/book").json()
    assert next(r for r in rows if r["sku_code"] == "SKU-SNK-011")["current_price"] == 12.5

    # history kept both directions, no hole in as-of
    hist = client.get("/api/prices/history", params={"sku_code": SKU}).json()
    seq = [(h["source_ticket_no"], h["price"]) for h in reversed(hist)]
    assert (bad["ticket_no"], 120.0) in seq
    assert (draft["ticket_no"], 28.0) in seq
    assert client.get("/api/compensation/gates").json()["blocked_skus"] == []


def test_auto_draft_batch_reverse_target_uses_pre_batch_price(client):
    """Same SKU touched by two tickets in one batch: the reverse target must be
    the price in force before the batch's first write, not an intermediate one."""
    client.put("/api/compensation/strategy", json={"compensation_strategy": "auto_draft"})
    _set_clock(client, "2026-11-16T09:00:00Z")
    _make_ticket(client, "2026-11-16T09:20:00Z", price=99.0, title="批次内第一次改价")
    _make_ticket(client, "2026-11-16T09:40:00Z", price=120.0, title="批次内第二次改价")
    _set_clock(client, "2026-11-16T10:00:00Z")
    _apply_due(client)
    assert _book_price(client) == 120.0

    batch = client.get("/api/apply/batches").json()[0]
    payload = client.post(
        "/api/compensation/markers",
        json={"target_type": "batch", "target_id": batch["id"]},
    ).json()
    target_prices = {l["sku_code"]: l["new_price"] for l in payload["draft_ticket"]["lines"]}
    # reverse all the way back to the pre-batch list price, not the 99 mid-price
    assert target_prices[SKU] == 28.0


def test_partial_compensation_keeps_gate_open(client):
    """Marker covering two SKUs stays open until every SKU is compensated."""
    _set_clock(client, "2026-11-16T09:00:00Z")
    r = client.post(
        "/api/tickets",
        json={
            "title": "双SKU误用",
            "effective_at": "2026-11-16T09:30:00Z",
            "lines": [
                {"sku_code": SKU, "new_price": 120.0},
                {"sku_code": "SKU-SNK-011", "new_price": 55.0},
            ],
        },
    )
    assert r.status_code == 200
    _set_clock(client, "2026-11-16T10:00:00Z")
    _apply_due(client)
    bad_id = r.json()["id"]

    r = client.post(
        "/api/compensation/markers",
        json={"target_type": "ticket", "target_id": bad_id},
    )
    marker = r.json()["marker"]
    assert set(marker["sku_codes"]) == {SKU, "SKU-SNK-011"}

    # compensate only one SKU
    r = client.post(
        "/api/compensation/tickets",
        json={
            "marker_id": marker["id"],
            "effective_at": "2026-11-16T11:00:00Z",
            "lines": [{"sku_code": SKU, "new_price": 28.0}],
        },
    )
    assert r.status_code == 200
    _set_clock(client, "2026-11-16T11:30:00Z")
    res = _apply_due(client)
    assert res["resolved_marker_ids"] == []

    still = client.get(f"/api/compensation/markers/{marker['id']}").json()
    assert still["status"] == "open"
    # marker still open => both SKUs remain on the gate (gates derive from
    # open markers, not from per-SKU compensation progress)
    blocked = client.get("/api/compensation/gates").json()["blocked_skus"]
    assert set(blocked) == {SKU, "SKU-SNK-011"}
    # but the compensation write itself did land on the price book
    assert _book_price(client) == 28.0

    # finish the second SKU -> marker resolves
    r = client.post(
        "/api/compensation/tickets",
        json={
            "marker_id": marker["id"],
            "effective_at": "2026-11-16T12:00:00Z",
            "lines": [{"sku_code": "SKU-SNK-011", "new_price": 12.5}],
        },
    )
    assert r.status_code == 200
    _set_clock(client, "2026-11-16T12:30:00Z")
    res = _apply_due(client)
    assert marker["id"] in res["resolved_marker_ids"]
    assert client.get("/api/compensation/gates").json()["blocked_skus"] == []


def test_dashboard_and_ticket_listing_expose_compensation_state(client):
    _set_clock(client, "2026-11-16T09:00:00Z")
    bad = _make_ticket(client, "2026-11-16T09:30:00Z", price=99.0)
    _set_clock(client, "2026-11-16T10:00:00Z")
    _apply_due(client)
    client.post(
        "/api/compensation/markers",
        json={"target_type": "ticket", "target_id": bad["id"]},
    )
    dash = client.get("/api/dashboard").json()
    assert dash["open_marker_count"] == 1
    assert dash["gated_sku_count"] == 1
    assert dash["compensation_strategy"] == "manual"

    listing = client.get("/api/tickets").json()
    assert all("kind" in t for t in listing)
    assert next(t for t in listing if t["id"] == bad["id"])["kind"] == "normal"


def test_no_rollback_or_delete_endpoints_exist(client):
    # The API surface must offer no way to silently roll back/delete a
    # successful batch, book write or history row.
    paths = {route.path for route in client.app.routes}
    for p in paths:
        assert "delete" not in p.lower()
        assert "rollback" not in p.lower()
        assert "revert" not in p.lower()
    # no DELETE method on any route
    for route in client.app.routes:
        assert "DELETE" not in getattr(route, "methods", set())


# ---------------------------------------------------------------------------
# Adversarial closing-rule tests: the gate is recomputed from compensation
# facts + price-tag source, never from an independent switch.
# ---------------------------------------------------------------------------

def _raw_conn():
    import sqlite3

    from app import config

    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _prepare_gated_marker(client):
    """One applied normal ticket (misuse), plus an already-scheduled normal
    ticket that only the apply-time recheck can catch, then an open marker."""
    _set_clock(client, "2026-11-16T09:00:00Z")
    bad = _make_ticket(client, "2026-11-16T09:30:00Z", price=99.0)
    blocked = _make_ticket(client, "2026-11-16T10:15:00Z", price=77.0, title="先建后闸单")
    _set_clock(client, "2026-11-16T10:00:00Z")
    _apply_due(client)
    assert _book_price(client) == 99.0
    r = client.post(
        "/api/compensation/markers",
        json={"target_type": "ticket", "target_id": bad["id"]},
    )
    assert r.status_code == 200, r.text
    return bad, r.json()["marker"], blocked


def test_gate_open_normal_apply_never_lands_on_price_tag(client):
    """Path A '闸开仍普通应用成功': while the gate is open a due normal ticket
    must be refused at apply time — no price-book/history write, stays
    scheduled, reported in skipped_gated; build-time rejection also holds."""
    _, marker, blocked = _prepare_gated_marker(client)

    # build-time refusal
    r = client.post(
        "/api/tickets",
        json={
            "title": "禁闸期普通单",
            "effective_at": "2026-11-16T10:30:00Z",
            "lines": [{"sku_code": SKU, "new_price": 88.0}],
        },
    )
    assert r.status_code == 409

    # the pre-existing scheduled ticket can only be caught at apply time
    _set_clock(client, "2026-11-16T11:00:00Z")
    res = _apply_due(client)
    assert res["applied"] is False
    assert [s["ticket_id"] for s in res["skipped_gated"]] == [blocked["id"]]
    assert client.get(f"/api/tickets/{blocked['id']}").json()["status"] == "scheduled"

    # price tag must still be the misuse price, and its source is not the
    # blocked normal ticket (three-way cross evidence)
    assert _book_price(client) == 99.0
    book = next(r for r in client.get("/api/prices/book").json() if r["sku_code"] == SKU)
    assert book["source_ticket_no"] != blocked["ticket_no"]
    hist = client.get("/api/prices/history", params={"sku_code": SKU}).json()
    assert all(h["source_ticket_no"] != blocked["ticket_no"] for h in hist)

    # gate endpoint agrees with the marker table
    gates = client.get("/api/compensation/gates").json()
    assert gates["blocked_skus"] == [SKU]
    assert gates["gates"][0]["marker_id"] == marker["id"]


def test_resolved_without_compensation_is_forced_back_closed(client):
    """Path B '无补偿却显示已解除': flip the marker status to resolved with no
    applied compensation. Every gate read must recompute from facts, force it
    back open, refuse normal applies and flag the divergence."""
    _, marker, blocked = _prepare_gated_marker(client)

    # lie directly at the storage layer (no API exists to do this)
    conn = _raw_conn()
    conn.execute(
        "UPDATE misapply_markers SET status='resolved', resolved_at='2026-11-16T10:30:00Z' WHERE id=?",
        (marker["id"],),
    )
    conn.commit()
    conn.close()

    integrity = client.get("/api/compensation/gates/integrity").json()
    kinds = {d["kind"] for d in integrity["divergences"]}
    assert "resolved_without_compensation" in kinds
    # read-only check does not repair by itself
    conn = _raw_conn()
    assert conn.execute(
        "SELECT status FROM misapply_markers WHERE id=?", (marker["id"],)
    ).fetchone()["status"] == "resolved"
    conn.close()

    # build-time entry never trusts the lied column either: it recomputes the
    # gate from facts and refuses before any repair endpoint was touched
    r = client.post(
        "/api/tickets",
        json={
            "title": "谎言闸下的普通单",
            "effective_at": "2026-11-16T10:45:00Z",
            "lines": [{"sku_code": SKU, "new_price": 50.0}],
        },
    )
    assert r.status_code == 409
    assert client.get("/api/dashboard").json()["gated_sku_count"] == 1

    # the gate endpoint is a closing point: it recomputes and re-closes
    gates = client.get("/api/compensation/gates").json()
    assert gates["blocked_skus"] == [SKU]
    assert gates["reopened_marker_ids"] == [marker["id"]]
    conn = _raw_conn()
    row = conn.execute(
        "SELECT status, resolved_at, resolved_ticket_id FROM misapply_markers WHERE id=?",
        (marker["id"],),
    ).fetchone()
    conn.close()
    assert row["status"] == "open"
    assert row["resolved_at"] is None
    assert row["resolved_ticket_id"] is None

    # normal apply still refused; lifting requires a real applied compensation
    _set_clock(client, "2026-11-16T11:00:00Z")
    res = _apply_due(client)
    assert [s["ticket_id"] for s in res["skipped_gated"]] == [blocked["id"]]
    assert _book_price(client) == 99.0

    r = client.post(
        "/api/compensation/tickets",
        json={
            "marker_id": marker["id"],
            "effective_at": "2026-11-16T12:00:00Z",
            "lines": [{"sku_code": SKU, "new_price": 28.0}],
        },
    )
    assert r.status_code == 200
    _set_clock(client, "2026-11-16T12:30:00Z")
    res = _apply_due(client)
    assert marker["id"] in res["resolved_marker_ids"]
    assert _book_price(client) == 28.0
    assert client.get("/api/compensation/gates").json()["blocked_skus"] == []
    assert client.get("/api/compensation/gates/integrity").json()["divergent"] is False
    # and only now does the held normal ticket land
    _apply_due(client)
    assert client.get(f"/api/tickets/{blocked['id']}").json()["status"] == "applied"


def test_compensated_but_still_gated_is_lifted_by_facts(client):
    """Path C '已补偿仍拦截': apply a fully covering compensation, then force
    the marker back to open by hand. Facts must lift the gate again and the
    legal follow-up normal ticket must apply."""
    _, marker, _ = _prepare_gated_marker(client)
    r = client.post(
        "/api/compensation/tickets",
        json={
            "marker_id": marker["id"],
            "effective_at": "2026-11-16T12:00:00Z",
            "lines": [{"sku_code": SKU, "new_price": 28.0}],
        },
    )
    comp = r.json()
    assert r.status_code == 200
    _set_clock(client, "2026-11-16T12:30:00Z")
    _apply_due(client)
    assert _book_price(client) == 28.0
    assert client.get("/api/compensation/gates").json()["blocked_skus"] == []

    # tamper: pretend the gate is still closed despite applied compensation
    conn = _raw_conn()
    conn.execute(
        "UPDATE misapply_markers SET status='open', resolved_at=NULL, resolved_ticket_id=NULL WHERE id=?",
        (marker["id"],),
    )
    conn.commit()
    conn.close()

    integrity = client.get("/api/compensation/gates/integrity").json()
    assert any(d["kind"] == "compensated_but_still_gated" for d in integrity["divergences"])

    # facts win: gate recomputes as lifted and points at the applied evidence
    gates = client.get("/api/compensation/gates").json()
    assert gates["blocked_skus"] == []
    assert gates["resolved_marker_ids"] == [marker["id"]]
    detail = client.get(f"/api/compensation/markers/{marker['id']}").json()
    assert detail["status"] == "resolved"
    evidence = [t for t in detail["compensation_tickets"] if t["status"] == "applied"]
    assert evidence and evidence[0]["ticket_no"] == comp["ticket_no"]

    # price-tag source already comes from the compensation ticket
    book = next(r for r in client.get("/api/prices/book").json() if r["sku_code"] == SKU)
    assert book["source_ticket_no"] == comp["ticket_no"]

    # legal follow-up normal ticket now applies
    legal = _make_ticket(client, "2026-11-16T13:00:00Z", price=31.5)
    _set_clock(client, "2026-11-16T13:30:00Z")
    _apply_due(client)
    assert client.get(f"/api/tickets/{legal['id']}").json()["status"] == "applied"
    assert _book_price(client) == 31.5
    assert client.get("/api/compensation/gates/integrity").json()["divergent"] is False


def test_partial_compensation_keeps_gate_closed_until_full_coverage(client):
    """A partial compensation applies its price-tag rows but the gate must not
    lift until every flagged SKU has applied compensation evidence."""
    _set_clock(client, "2026-11-16T09:00:00Z")
    r = client.post(
        "/api/tickets",
        json={
            "title": "双SKU误用",
            "effective_at": "2026-11-16T09:30:00Z",
            "lines": [
                {"sku_code": SKU, "new_price": 120.0},
                {"sku_code": "SKU-SNK-011", "new_price": 55.0},
            ],
        },
    )
    _set_clock(client, "2026-11-16T10:00:00Z")
    _apply_due(client)
    marker = client.post(
        "/api/compensation/markers",
        json={"target_type": "ticket", "target_id": r.json()["id"]},
    ).json()["marker"]

    client.post(
        "/api/compensation/tickets",
        json={
            "marker_id": marker["id"],
            "effective_at": "2026-11-16T11:00:00Z",
            "lines": [{"sku_code": SKU, "new_price": 28.0}],
        },
    )
    _set_clock(client, "2026-11-16T11:30:00Z")
    res = _apply_due(client)
    assert res["resolved_marker_ids"] == []
    gates = client.get("/api/compensation/gates").json()
    gate = next(g for g in gates["gates"] if g["marker_id"] == marker["id"])
    assert gate["covered_skus"] == [SKU]
    assert gate["missing_skus"] == ["SKU-SNK-011"]
    assert set(gates["blocked_skus"]) == {SKU, "SKU-SNK-011"}

    # finish coverage -> facts lift the gate
    client.post(
        "/api/compensation/tickets",
        json={
            "marker_id": marker["id"],
            "effective_at": "2026-11-16T12:00:00Z",
            "lines": [{"sku_code": "SKU-SNK-011", "new_price": 12.5}],
        },
    )
    _set_clock(client, "2026-11-16T12:30:00Z")
    res = _apply_due(client)
    assert marker["id"] in res["resolved_marker_ids"]
    assert client.get("/api/compensation/gates").json()["blocked_skus"] == []


def test_bypassed_normal_write_after_marking_is_reported(client):
    """If a normal ticket's price-tag source lands after the marker was created
    (gate bypass), the integrity cross-check must expose it even though the
    append-only history cannot be rewritten."""
    _, marker, blocked = _prepare_gated_marker(client)

    conn = _raw_conn()
    # applied_at / recorded_at are wall-clock timestamps (same clock the marker
    # created_at uses); the forged bypass happens strictly after marking.
    bypass_at = "2027-01-01T00:00:00Z"
    conn.execute(
        "UPDATE price_tickets SET status='applied', applied_at=?, apply_batch_id=NULL WHERE id=?",
        (bypass_at, blocked["id"]),
    )
    conn.execute(
        '''
        INSERT INTO price_history(sku_code, price, source_ticket_no, effective_at, recorded_at)
        VALUES (?,?,?,?,?)
        ''',
        (SKU, 77.0, blocked["ticket_no"], "2026-11-16T10:15:00Z", bypass_at),
    )
    conn.execute(
        '''
        UPDATE price_book SET current_price=77.0, source_ticket_no=?, effective_at=?, updated_at=?
        WHERE sku_code=?
        ''',
        (blocked["ticket_no"], "2026-11-16T10:15:00Z", bypass_at, SKU),
    )
    conn.commit()
    conn.close()

    integrity = client.get("/api/compensation/gates/integrity").json()
    assert integrity["divergent"] is True
    assert any(
        d["kind"] == "gated_sku_price_tag_from_normal_ticket"
        and d["source_ticket_no"] == blocked["ticket_no"]
        for d in integrity["divergences"]
    )
    # the gate itself stays closed: a normal ticket existing in the book does
    # not count as compensation evidence
    assert client.get("/api/compensation/gates").json()["blocked_skus"] == [SKU]


def test_no_independent_gate_switch_table_or_endpoint(client):
    """The legacy gate_shadow switch table must be gone and no API can lift a
    gate except applying a compensation ticket."""
    conn = _raw_conn()
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert "gate_shadow" not in names

    paths = {route.path for route in client.app.routes}
    for p in paths:
        assert "shadow" not in p.lower()
        assert "force" not in p.lower()
        assert "lift" not in p.lower()
        assert "open-gate" not in p.lower()


def test_legacy_shadow_table_is_dropped_on_schema_init(client):
    """A database created by the old code (with the toggleable gate_shadow
    table) loses that table on schema init, so the switch can never come back
    and the gate can only be derived from facts."""
    conn = _raw_conn()
    conn.execute(
        "CREATE TABLE gate_shadow (marker_id INTEGER PRIMARY KEY, blocked_skus TEXT NOT NULL, open INTEGER NOT NULL DEFAULT 1)"
    )
    conn.execute(
        "INSERT INTO gate_shadow(marker_id, blocked_skus, open) VALUES (999, ?, 0)",
        ('["SKU-TEA-001"]',),
    )
    conn.commit()
    conn.close()

    from app.seed import init_schema

    conn = _raw_conn()
    init_schema(conn)
    conn.commit()
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert "gate_shadow" not in names

    # the seeded SKU is not gated by the vanished 'open=0' switch row
    assert client.get("/api/compensation/gates").json()["blocked_skus"] == []
