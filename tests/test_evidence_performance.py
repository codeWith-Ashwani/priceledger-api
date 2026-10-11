"""Query growth and evidence-boundary regressions on an isolated database."""

from contextlib import contextmanager
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from app.catalog import coverage
from app.config import get_settings
from app.db import now, utc
from app.evidence import observation_rows
from app.insights import insights
from app.models import (
    CollectionAttempt,
    CollectionBudget,
    CollectionJob,
    Observation,
    Offer,
    Product,
    ProductMatch,
)


@contextmanager
def captured_selects(db):
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)


def add_peer(db, index):
    product_id, offer_id = f"peer-{index:03}", f"peer-offer-{index:03}"
    db.add(
        Product(
            id=product_id,
            title="Isolated exact match",
            brand="Test",
            category="electronics",
            variant_label="128 GB",
            attributes={"storage_gb": 128},
            illustration="product",
            is_demo=False,
        )
    )
    db.flush()
    db.add(
        Offer(
            id=offer_id,
            product_id=product_id,
            retailer_id="shop",
            source_id="test",
            external_id=offer_id,
            seller="Merchant",
            current_in_stock=True,
            current_item_paise=10000,
            current_shipping_paise=0,
            current_observed_at=now(),
        )
    )
    left, right = sorted(("phone-128", product_id))
    db.add(ProductMatch(left_id=left, right_id=right, method="test", evidence={}))
    db.flush()
    return product_id, offer_id


def test_coverage_query_count_does_not_grow_per_offer_and_respects_check_permissions(db):
    db.expunge_all()
    with captured_selects(db) as small:
        assert len(coverage("phone-128", db)["stores"]) == 1
    ids = ["offer-128"] + [add_peer(db, index)[1] for index in range(40)]
    at = now()
    blocked_until = at + timedelta(hours=2)
    db.add(
        CollectionBudget(
            source_id="test",
            budget_day=at.date().isoformat(),
            reserved_requests=0,
            blocked_until=blocked_until,
        )
    )
    for offer_id in ids:
        db.add(CollectionJob(offer_id=offer_id, status="queued", next_run_at=at))
        for source, delta, outcome, code in [
            ("test", -2, "success", None),
            ("test", -1, "failure", "approved_failure"),
            ("blocked", 0, "failure", "revoked_failure"),
            ("test", 1, "failure", "future_failure"),
        ]:
            db.add(
                CollectionAttempt(
                    offer_id=offer_id,
                    source_id=source,
                    product_url="https://example.org/product",
                    outcome=outcome,
                    started_at=at + timedelta(hours=delta),
                    error_code=code,
                )
            )
    db.commit()
    db.expunge_all()
    with captured_selects(db) as large:
        result = coverage("phone-128", db)
    assert len(result["stores"]) == 41
    assert len(large) <= len(small) + 2
    assert len(large) < 15
    assert all(
        store["last_check"]["error_code"] == "approved_failure" for store in result["stores"]
    )
    assert all(store["next_eligible_check_at"] == blocked_until for store in result["stores"])


def test_insights_does_not_load_collection_checks_or_build_chart_responses(db):
    db.expunge_all()
    with captured_selects(db) as queries:
        result = insights("phone-128", days=90, basis="item", db=db)
    assert result["observed_days"] == 0
    assert result["current"]["price_paise"] == 10000
    assert not any("collection_attempts" in sql.lower() for sql in queries)
    assert len(queries) <= 5


def test_observation_limit_is_global_across_database_batches(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_history_observations", 1000)
    at = now()
    members = []
    for index in range(101):
        product_id, offer_id = add_peer(db, index)
        members.append(product_id)
        db.add_all(
            [
                Observation(
                    offer_id=offer_id,
                    source_id="test",
                    external_event_id=f"{index}-{point}",
                    fingerprint="a" * 64,
                    observed_at=at - timedelta(minutes=point),
                    item_paise=10000,
                    shipping_paise=0,
                    in_stock=True,
                )
                for point in range(10)
            ]
        )
    db.commit()
    with pytest.raises(HTTPException) as failure:
        observation_rows(db, members, at - timedelta(days=1), at)
    assert failure.value.status_code == 422
    assert "shorter period" in failure.value.detail
    assert len(observation_rows(db, members[:100], at - timedelta(days=1), at)) == 1000
    monkeypatch.setattr(get_settings(), "max_history_observations", 2000)
    combined = observation_rows(db, members, at - timedelta(days=1), at)
    assert len(combined) == 1010
    ordering = [(utc(row[0].observed_at), row[0].id) for row in combined]
    assert ordering == sorted(ordering)


def test_future_current_quote_cannot_win_and_offline_quote_cannot_drive_online_assessment(
    client, db
):
    offer = db.get(Offer, "offer-128")
    offer.current_observed_at = now() + timedelta(hours=1)
    db.commit()
    assert client.get("/api/v1/products/phone-128").json()["offers"][0]["stale"]
    assert client.get("/api/v1/products/phone-128/assessment").json()["current_paise"] is None
    offer.current_observed_at = now() - timedelta(hours=1)
    offer.channel = "offline"
    db.commit()
    assert client.get("/api/v1/products/phone-128/assessment").json()["current_paise"] is None
