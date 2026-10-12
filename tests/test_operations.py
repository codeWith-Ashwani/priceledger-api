from datetime import timedelta

import pytest
from sqlalchemy import event, func, select

from app.config import get_settings
from app.db import now
from app.models import (
    CollectionAttempt,
    CollectionBudget,
    CollectionJob,
    Observation,
    Offer,
    Source,
)


@pytest.fixture
def owner(client, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_token", "operations-test-only")
    db.get(Source, "test").kind = "public_page"
    db.get(Offer, "offer-128").product_url = "https://shop.example.org/products/phone"
    db.commit()
    return {"X-Admin-Token": "operations-test-only"}


@pytest.mark.parametrize("path", ["overview", "offers", "sources"])
def test_operations_require_owner_auth_and_do_not_echo_credentials(client, monkeypatch, path):
    monkeypatch.setattr(get_settings(), "admin_token", "operations-test-only")
    assert client.get(f"/api/v1/admin/collection/{path}").status_code == 401
    response = client.get(
        f"/api/v1/admin/collection/{path}", headers={"X-Admin-Token": "incorrect"}
    )
    assert response.status_code == 401
    assert "incorrect" not in response.text


def test_empty_overview_and_read_only_unscheduled_gap(client, db, owner):
    response = client.get("/api/v1/admin/collection/overview", headers=owner)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    summary = response.json()["summary"]
    assert summary == {
        "tracked": 1,
        "stale": 0,
        "gap": 1,
        "failed": 0,
        "due": 0,
        "running": 0,
        "paused": 0,
        "unscheduled": 1,
        "inactive": 0,
    }
    row = client.get("/api/v1/admin/collection/offers?status=gap", headers=owner).json()["items"][0]
    assert row["last_evidence_at"] is None
    assert row["job_status"] is None
    assert row["current_observed_at"].endswith("+00:00")
    assert all(row[key] is False for key in ("failed", "due", "running", "paused", "lease_expired"))
    assert db.scalar(select(func.count()).select_from(CollectionJob)) == 0
    db.get(Source, "test").kind = "merchant"
    db.commit()
    summary = client.get("/api/v1/admin/collection/overview", headers=owner).json()["summary"]
    assert all(value == 0 for value in summary.values())


def test_failure_gap_budget_and_permissions_remain_separate(client, db, owner, monkeypatch):
    at = now()
    monkeypatch.setattr("app.operations.now", lambda: at)
    db.add(
        CollectionJob(
            offer_id="offer-128",
            status="queued",
            next_run_at=at - timedelta(hours=1),
            consecutive_failures=1,
            last_error_code="retailer_blocked",
        )
    )
    db.add(
        CollectionBudget(
            source_id="test",
            budget_day=at.date().isoformat(),
            reserved_requests=10,
            blocked_until=at + timedelta(hours=2),
        )
    )
    db.add(
        Observation(
            offer_id="offer-128",
            source_id="test",
            external_event_id="old",
            fingerprint="a" * 64,
            observed_at=at - timedelta(hours=60),
            item_paise=10000,
            in_stock=True,
        )
    )
    db.add(
        CollectionAttempt(
            offer_id="offer-128",
            source_id="test",
            product_url="https://shop.example.org/products/phone",
            started_at=at - timedelta(hours=1),
            completed_at=at - timedelta(minutes=59),
            outcome="failure",
            error_code="retailer_blocked",
        )
    )
    db.commit()
    summary = client.get("/api/v1/admin/collection/overview", headers=owner).json()
    assert summary["summary"]["gap"] == summary["summary"]["failed"] == 1
    assert summary["summary"]["stale"] == summary["summary"]["due"] == 0
    assert summary["checks_24h"]["failed"] == 1
    row = client.get("/api/v1/admin/collection/offers?status=failed", headers=owner).json()[
        "items"
    ][0]
    assert row["remaining_requests"] == get_settings().collection_daily_budget - 10
    assert row["last_check_error"] == "retailer_blocked"
    budget = db.get(CollectionBudget, "test")
    budget.blocked_until = at - timedelta(seconds=1)
    db.commit()
    assert (
        client.get("/api/v1/admin/collection/offers?status=due", headers=owner).json()["total"] == 1
    )
    db.get(Source, "test").history_allowed = False
    db.commit()
    assert (
        client.get("/api/v1/admin/collection/offers?status=due", headers=owner).json()["total"] == 0
    )
    assert (
        client.get("/api/v1/admin/collection/offers?status=inactive", headers=owner).json()["total"]
        == 1
    )


def test_approved_history_and_future_prices_are_not_confused_with_collection_success(
    client, db, owner
):
    at = now()
    db.get(Offer, "offer-128").current_observed_at = at + timedelta(hours=1)
    for source, delta in [("test", -60), ("blocked", -1), ("test", 1)]:
        db.add(
            Observation(
                offer_id="offer-128",
                source_id=source,
                external_event_id=f"{source}-{delta}",
                fingerprint="a" * 64,
                observed_at=at + timedelta(hours=delta),
                item_paise=10000,
                in_stock=True,
            )
        )
    db.commit()
    summary = client.get("/api/v1/admin/collection/overview", headers=owner).json()["summary"]
    assert summary["stale"] == summary["gap"] == 1


def test_unpriced_check_does_not_close_a_price_history_gap(client, db, owner):
    db.get(Offer, "offer-128").current_item_paise = None
    db.add(
        Observation(
            offer_id="offer-128",
            source_id="test",
            external_event_id="unpriced",
            fingerprint="b" * 64,
            observed_at=now(),
            item_paise=None,
            in_stock=False,
        )
    )
    db.commit()
    summary = client.get("/api/v1/admin/collection/overview", headers=owner).json()["summary"]
    assert summary["stale"] == summary["gap"] == 1


def test_expired_leases_and_daily_budget_rollover_match_worker_eligibility(
    client, db, owner, monkeypatch
):
    at = now()
    monkeypatch.setattr("app.operations.now", lambda: at)
    db.add(
        CollectionJob(
            offer_id="offer-128",
            status="running",
            next_run_at=at,
            lease_expires_at=at - timedelta(minutes=1),
        )
    )
    db.add(
        CollectionBudget(
            source_id="test",
            budget_day=(at - timedelta(days=1)).date().isoformat(),
            reserved_requests=get_settings().collection_daily_budget,
        )
    )
    db.commit()
    response = client.get("/api/v1/admin/collection/offers?status=due", headers=owner).json()
    assert response["total"] == 1 and response["items"][0]["lease_expired"]
    db.get(CollectionBudget, "test").budget_day = at.date().isoformat()
    db.commit()
    assert (
        client.get("/api/v1/admin/collection/offers?status=due", headers=owner).json()["total"] == 0
    )


def test_source_health_includes_search_failures_without_a_tracked_offer(client, db, owner):
    at = now()
    db.add(
        Source(
            id="search-only",
            name="Search only",
            kind="public_page",
            display_allowed=True,
            history_allowed=True,
        )
    )
    db.flush()
    db.add(
        CollectionAttempt(
            source_id="search-only",
            product_url="https://shop.example.org/search?q=phone",
            started_at=at - timedelta(minutes=2),
            completed_at=at - timedelta(minutes=1),
            outcome="failure",
            error_code="retailer_blocked",
        )
    )
    db.commit()
    rows = client.get("/api/v1/admin/collection/sources", headers=owner).json()["items"]
    row = next(row for row in rows if row["id"] == "search-only")
    assert row["tracked"] == 0 and row["failed_24h"] == row["completed_24h"] == 1


def test_pagination_filters_validation_and_constant_query_count(client, db, owner):
    for index in range(35):
        db.add(
            Offer(
                id=f"owner-offer-{index:02}",
                product_id="phone-128",
                source_id="test",
                retailer_id="shop",
                external_id=f"owner-{index}",
                seller="Merchant",
                product_url=f"https://shop.example.org/products/{index}",
            )
        )
    db.commit()
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        result = client.get("/api/v1/admin/collection/offers?limit=10&offset=10", headers=owner)
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)
    assert result.status_code == 200
    assert result.json()["total"] == 36 and len(result.json()["items"]) == 10
    assert len(statements) <= 3
    assert (
        client.get("/api/v1/admin/collection/offers?status=unknown", headers=owner).status_code
        == 422
    )
    assert client.get("/api/v1/admin/collection/offers?limit=101", headers=owner).status_code == 422
    assert (
        client.get("/api/v1/admin/collection/sources?offset=-1", headers=owner).status_code == 422
    )


def test_expired_source_and_future_completed_check_do_not_qualify(client, db, owner):
    at = now()
    db.get(Source, "test").expires_at = at - timedelta(minutes=1)
    db.add(
        CollectionAttempt(
            offer_id="offer-128",
            source_id="test",
            product_url="https://shop.example.org/products/phone",
            started_at=at - timedelta(minutes=1),
            completed_at=at + timedelta(hours=1),
            outcome="failure",
            error_code="future-check",
        )
    )
    db.commit()
    row = client.get("/api/v1/admin/collection/offers", headers=owner).json()["items"][0]
    assert row["inactive"] and not row["source_active"]
    assert row["last_check_at"] is None and row["failed"] is False
    overview = client.get("/api/v1/admin/collection/overview", headers=owner).json()
    assert overview["checks_24h"]["completed"] == 0
    sources = client.get("/api/v1/admin/collection/sources?limit=1&offset=0", headers=owner).json()
    assert sources["total"] == 1 and sources["items"][0]["active"] is False
    assert (
        client.get("/api/v1/admin/collection/sources?limit=1&offset=1", headers=owner).json()[
            "items"
        ]
        == []
    )
