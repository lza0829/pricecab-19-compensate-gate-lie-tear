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
