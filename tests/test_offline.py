"""Controlled offline evidence lives only in test databases."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db import now
from app.models import Offer, OfflineQuote, OfflineQuoteEvent, OfflineShop, Product, Source


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_token", "offline-test-only")
    monkeypatch.setattr(get_settings(), "admin_requests_per_minute", 1000)
    monkeypatch.setattr(get_settings(), "read_requests_per_minute", 10000)
    return {"X-Admin-Token": "offline-test-only"}


def shop(client, owner, name="Contract Shop A", city="Contract City", area="Market Road"):
    body = {
        "request_id": str(uuid4()),
        "name": name,
        "city": city,
        "state": "Contract State",
        "area": area,
        "approval_reference": "PRIVATE operator permission",
        "rights_confirmed": True,
    }
    response = client.post("/api/v1/admin/offline/shops", json=body, headers=owner)
    assert response.status_code == 201, response.text
    return response.json()["id"], body


def draft(
    client, owner, shop_id, *, price=10000, observed=None, valid=None, stock=True, condition="new"
):
    at = observed or now() - timedelta(minutes=1)
    body = {
        "request_id": str(uuid4()),
        "product_id": "phone-128",
        "shop_id": shop_id,
        "variant_attributes": {"storage_gb": 128},
        "item_paise": price,
        "condition": condition,
        "in_stock": stock,
        "observed_at": at.isoformat(),
        "valid_until": (valid or at + timedelta(days=2)).isoformat(),
        "evidence_reference": "PRIVATE receipt reference",
        "exact_variant_confirmed": True,
        "includes_tax": True,
        "unconditional_price": True,
    }
    response = client.post("/api/v1/admin/offline/quotes", json=body, headers=owner)
    assert response.status_code == 201, response.text
    return response.json()["quote"], body


def review(client, owner, quote, action="publish", request_id=None):
    body = {
        "request_id": request_id or str(uuid4()),
        "expected_revision": quote["revision"],
        "action": action,
        "note": "PRIVATE owner evidence check",
    }
    response = client.post(
        f"/api/v1/admin/offline/quotes/{quote['id']}/review", json=body, headers=owner
    )
    assert response.status_code == 200, response.text
    return response.json(), body


def public(client, **params):
    response = client.get("/api/v1/products/phone-128/offline-quotes", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_authenticated_draft_review_publication_and_private_evidence(client, db, owner):
    shop_id, _ = shop(client, owner)
    quote, _ = draft(client, owner, shop_id)
    assert public(client)["items"] == []
    published, _ = review(client, owner, quote)
    result = public(client)
    assert result["summary"] == {
        "current_shop_count": 1,
        "range_ready": False,
        "low_paise": None,
        "high_paise": None,
    }
    assert result["items"][0]["current"] and result["items"][0]["item_paise"] == 10000
    assert "PRIVATE" not in str(result)
    assert "evidence_reference" not in str(result) and "product_snapshot" not in str(result)
    detail = client.get(f"/api/v1/admin/offline/quotes/{quote['id']}", headers=owner).json()
    assert detail["events"]["total"] == 2 and detail["quote"]["status"] == "published"
    assert detail["quote"]["shop_name"] == "Contract Shop A"
    assert detail["quote"]["area"] == "Market Road"
    assert published["revision"] == 2
    assert db.get(Offer, "offer-128").current_item_paise == 10000


def test_two_shops_make_range_and_latest_stock_check_masks_older_available_quote(client, owner):
    first, _ = shop(client, owner)
    second, _ = shop(client, owner, "Contract Shop B")
    q, _ = draft(client, owner, first, price=10000)
    review(client, owner, q)
    q, _ = draft(client, owner, second, price=12000)
    review(client, owner, q)
    result = public(client)
    assert result["summary"] == {
        "current_shop_count": 2,
        "range_ready": True,
        "low_paise": 10000,
        "high_paise": 12000,
    }
    q, _ = draft(
        client, owner, first, price=5000, observed=now() - timedelta(seconds=10), stock=False
    )
    review(client, owner, q)
    result = public(client)
    assert result["summary"]["current_shop_count"] == 1 and not result["summary"]["range_ready"]
    assert sum(x["current"] for x in result["items"]) == 1


def test_latest_quote_per_shop_excludes_duplicates_backfills_and_withdrawals(client, owner):
    sid, _ = shop(client, owner)
    q, _ = draft(client, owner, sid, price=12000)
    published, _ = review(client, owner, q)
    old, _ = draft(client, owner, sid, price=100, observed=now() - timedelta(days=1))
    review(client, owner, old)
    result = public(client)
    assert result["summary"]["current_shop_count"] == 1
    assert [x["item_paise"] for x in result["items"] if x["current"]] == [12000]
    review(client, owner, {**q, "revision": published["revision"]}, "withdraw")
    result = public(client)
    assert result["summary"]["current_shop_count"] == 0 and result["total"] == 1
    assert not result["items"][0]["current"]


def test_withdrawing_an_unpublished_draft_cannot_mask_a_published_quote(client, owner):
    sid, _ = shop(client, owner)
    q, _ = draft(client, owner, sid)
    review(client, owner, q)
    pending, _ = draft(client, owner, sid, observed=now() - timedelta(seconds=10))
    review(client, owner, pending, "withdraw")
    assert public(client)["summary"]["current_shop_count"] == 1


@pytest.mark.parametrize(
    "mode",
    [
        "expired",
        "stale",
        "future",
        "revoked_display",
        "revoked_history",
        "source_expired",
        "wrong_kind",
        "variant_changed",
    ],
)
def test_ineligible_evidence_never_creates_a_current_range(client, db, owner, mode):
    sid, _ = shop(client, owner)
    q, _ = draft(client, owner, sid)
    review(client, owner, q)
    row = db.get(OfflineQuote, q["id"])
    source = db.get(Source, db.get(OfflineShop, sid).source_id)
    if mode == "expired":
        row.valid_until = now() - timedelta(seconds=1)
    elif mode == "stale":
        row.observed_at = now() - timedelta(days=8)
        row.valid_until = now() + timedelta(days=1)
    elif mode == "future":
        row.observed_at = now() + timedelta(days=1)
    elif mode == "revoked_display":
        source.display_allowed = False
    elif mode == "revoked_history":
        source.history_allowed = False
    elif mode == "source_expired":
        source.expires_at = now() - timedelta(seconds=1)
    elif mode == "wrong_kind":
        source.kind = "public_page"
    else:
        db.get(Product, "phone-128").attributes = {"storage_gb": 256}
    db.commit()
    result = public(client)
    assert result["summary"]["current_shop_count"] == 0
    assert not any(x["current"] for x in result["items"])


def test_cities_conditions_and_products_are_isolated_with_bounded_history(client, owner):
    sid, _ = shop(client, owner)
    other, _ = shop(client, owner, "Contract Other", city="Other City")
    for shop_id, condition in [(sid, "new"), (sid, "used"), (other, "new")]:
        q, _ = draft(client, owner, shop_id, condition=condition)
        review(client, owner, q)
    result = public(client, limit=1)
    assert len(result["cities"]) == 2
    assert len(result["items"]) == 1 and result["summary"]["current_shop_count"] == 1
    other_city = next(x["city_id"] for x in result["cities"] if x["city_id"] != result["city_id"])
    assert public(client, city_id=other_city)["summary"]["current_shop_count"] == 1
    assert public(client, condition="used")["total"] == 1
    assert client.get("/api/v1/products/phone-256/offline-quotes").status_code == 404
    assert public(client, city_id="f" * 32)["items"] == []
    for params in [
        {"limit": 101},
        {"offset": -1},
        {"days": 0},
        {"city_id": "bad"},
        {"condition": "unknown"},
    ]:
        assert (
            client.get("/api/v1/products/phone-128/offline-quotes", params=params).status_code
            == 422
        )


def test_idempotent_shop_quote_and_review_retries_do_not_duplicate_evidence(client, db, owner):
    sid, sb = shop(client, owner)
    replayed = client.post("/api/v1/admin/offline/shops", json=sb, headers=owner)
    assert replayed.status_code == 200 and replayed.json()["replayed"]
    assert (
        client.post(
            "/api/v1/admin/offline/shops", json={**sb, "name": "Other"}, headers=owner
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/admin/offline/shops", json={**sb, "request_id": str(uuid4())}, headers=owner
        ).status_code
        == 409
    )
    q, body = draft(client, owner, sid)
    assert client.post("/api/v1/admin/offline/quotes", json=body, headers=owner).json()["replayed"]
    assert (
        client.post(
            "/api/v1/admin/offline/quotes", json={**body, "item_paise": 50}, headers=owner
        ).status_code
        == 409
    )
    result, rb = review(client, owner, q)
    response = client.post(f"/api/v1/admin/offline/quotes/{q['id']}/review", json=rb, headers=owner)
    assert response.json() == {**result, "replayed": True}
    assert (
        client.post(
            f"/api/v1/admin/offline/quotes/{q['id']}/review",
            json={**rb, "note": "Changed note"},
            headers=owner,
        ).status_code
        == 409
    )
    assert db.scalar(select(func.count()).select_from(OfflineQuoteEvent)) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("item_paise", True),
        ("item_paise", "100"),
        ("item_paise", -1),
        ("in_stock", "true"),
        ("includes_tax", 1),
        ("exact_variant_confirmed", False),
        ("unconditional_price", "true"),
        ("condition", "unknown"),
        ("observed_at", "2026-01-01T12:00:00"),
        ("observed_at", "2099-01-01T00:00:00Z"),
        ("valid_until", "2099-01-01T00:00:00Z"),
    ],
)
def test_invalid_quote_fields_are_rejected_without_writing(client, db, owner, field, value):
    sid, _ = shop(client, owner)
    _, body = draft(client, owner, sid)
    before = db.scalar(select(func.count()).select_from(OfflineQuote))
    body.update({"request_id": str(uuid4()), field: value})
    response = client.post("/api/v1/admin/offline/quotes", json=body, headers=owner)
    assert response.status_code == 422
    assert "PRIVATE" not in response.text
    assert db.scalar(select(func.count()).select_from(OfflineQuote)) == before


def test_publish_revalidates_permissions_and_variant_snapshot(client, db, owner):
    sid, _ = shop(client, owner)
    q, _ = draft(client, owner, sid)
    db.get(Product, "phone-128").attributes = {"storage_gb": 256}
    db.commit()
    body = {
        "request_id": str(uuid4()),
        "expected_revision": 1,
        "action": "publish",
        "note": "Evidence reviewed",
    }
    assert (
        client.post(
            f"/api/v1/admin/offline/quotes/{q['id']}/review", json=body, headers=owner
        ).status_code
        == 409
    )
    db.get(Source, db.get(OfflineShop, sid).source_id).display_allowed = False
    db.commit()
    assert (
        client.post(
            f"/api/v1/admin/offline/quotes/{q['id']}/review", json=body, headers=owner
        ).status_code
        == 403
    )


def test_owner_endpoints_reject_anonymous_access_and_empty_evidence_is_honest(client, owner):
    assert client.get("/api/v1/admin/offline/shops").status_code == 401
    assert client.get("/api/v1/admin/offline/quotes").status_code == 401
    result = public(client)
    assert result["cities"] == result["items"] == [] and result["total"] == 0
    assert not result["summary"]["range_ready"]


def test_city_labels_are_normalized_and_range_counts_every_shop_outside_page(client, owner):
    for name, city, price in [
        ("Contract A", "Contract City", 10000),
        ("Contract B", "contract CITY", 12000),
        ("Contract C", "Contract City", 9000),
    ]:
        sid, _ = shop(client, owner, name, city=city)
        q, _ = draft(client, owner, sid, price=price)
        review(client, owner, q)
    result = public(client, limit=1)
    assert len(result["cities"]) == 1 and result["total"] == 3
    assert len(result["items"]) == 1
    assert result["summary"] == {
        "current_shop_count": 3,
        "range_ready": True,
        "low_paise": 9000,
        "high_paise": 12000,
    }
    assert public(client, limit=1, offset=2)["summary"] == result["summary"]


def test_publication_and_reads_do_not_mutate_online_history_or_schedule_jobs(client, db, owner):
    from app.db import Base

    sid, _ = shop(client, owner)
    excluded = {"offline_shops", "offline_quotes", "offline_quote_events", "client_quotas"}

    def snapshot():
        return {
            table.name: [tuple(row) for row in db.execute(select(table))]
            for table in Base.metadata.sorted_tables
            if table.name not in excluded
        }

    before = snapshot()
    q, _ = draft(client, owner, sid)
    published, _ = review(client, owner, q)
    public(client)
    review(client, owner, {**q, "revision": published["revision"]}, "withdraw")
    public(client)
    assert snapshot() == before


def test_duplicate_shop_rolls_back_its_source_and_typed_variants_do_not_coerce(client, db, owner):
    sid, body = shop(client, owner)
    count = db.scalar(select(func.count()).select_from(Source))
    response = client.post(
        "/api/v1/admin/offline/shops",
        json={**body, "request_id": str(uuid4()), "name": "contract SHOP a"},
        headers=owner,
    )
    assert response.status_code == 409
    assert db.scalar(select(func.count()).select_from(Source)) == count
    _, body = draft(client, owner, sid)
    db.get(Product, "phone-128").attributes = {"storage_gb": True}
    db.commit()
    response = client.post(
        "/api/v1/admin/offline/quotes",
        json={**body, "request_id": str(uuid4()), "variant_attributes": {"storage_gb": 1}},
        headers=owner,
    )
    assert response.status_code == 409


def test_no_product_anchor_permission_and_no_demo_prices(client, db, owner):
    sid, _ = shop(client, owner)
    q, _ = draft(client, owner, sid)
    review(client, owner, q)
    db.get(Source, "test").history_allowed = False
    db.commit()
    assert client.get("/api/v1/products/phone-128/offline-quotes").status_code == 404
    db.get(Source, "test").history_allowed = True
    db.get(Product, "phone-128").is_demo = True
    db.commit()
    assert client.get("/api/v1/products/phone-128/offline-quotes").status_code == 404
