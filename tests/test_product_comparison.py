from datetime import timedelta

import pytest
from sqlalchemy import event, func, select

from app.config import get_settings
from app.db import now
from app.models import Observation, Offer, Product, ProductMatch, Source


@pytest.fixture
def selections(db):
    db.add(
        Offer(
            id="offer-256",
            product_id="phone-256",
            retailer_id="shop",
            source_id="test",
            external_id="external-256",
            seller="Merchant",
            current_item_paise=20000,
            current_shipping_paise=None,
            current_in_stock=True,
            current_observed_at=now(),
        )
    )
    db.commit()
    return {"product_ids": ["phone-256", "phone-128"]}


def test_independent_columns_preserve_order_prices_and_unknown_metadata(client, db, selections):
    result = client.post("/api/v1/product-comparison", json=selections)
    assert result.status_code == 200
    assert result.headers["Cache-Control"] == "no-store"
    data = result.json()
    assert data["scope"] == "independent_exact_variants"
    assert data["checked_at"].endswith("+00:00")
    left, right = data["products"]
    assert [left["id"], right["id"]] == selections["product_ids"]
    assert left["attributes"] == {"storage_gb": 256}
    assert left["best_item_price_paise"] == 20000 and left["best_price_paise"] is None
    assert right["best_item_price_paise"] == 10000 and right["best_price_paise"] == 10500
    assert left["offers"][0]["source_name"] == "Approved test source"
    assert left["offers"][0]["observed_at"].endswith("+00:00")
    assert db.scalar(select(func.count()).select_from(ProductMatch)) == 0
    assert db.scalar(select(func.count()).select_from(Observation)) == 0


@pytest.mark.parametrize(
    "ids",
    [
        [],
        ["phone-128"],
        ["phone-128"] * 5,
        ["phone-128", "phone-128"],
        [" phone-128 ", "phone-128"],
        [True, "phone-128"],
        ["x" * 81, "phone-128"],
    ],
)
def test_invalid_selections_are_rejected(client, ids):
    response = client.post("/api/v1/product-comparison", json={"product_ids": ids})
    assert response.status_code == 422


@pytest.mark.parametrize("change", ["missing", "demo", "revoked", "expired", "history", "no_offer"])
def test_all_columns_require_approved_non_demo_anchor_evidence(client, db, selections, change):
    if change == "missing":
        selections["product_ids"][0] = "missing"
    elif change == "demo":
        db.get(Product, "phone-256").is_demo = True
    elif change == "no_offer":
        db.delete(db.get(Offer, "offer-256"))
    else:
        source = db.get(Source, "test")
        if change == "revoked":
            source.display_allowed = False
        elif change == "expired":
            source.expires_at = now() - timedelta(hours=1)
        else:
            source.history_allowed = False
    db.commit()
    result = client.post("/api/v1/product-comparison", json=selections)
    assert result.status_code == 404
    assert "products" not in result.json()


def test_exact_aliases_cannot_be_compared_as_different_products(client, db, selections):
    db.add(ProductMatch(left_id="phone-128", right_id="phone-256", method="test", evidence={}))
    db.commit()
    result = client.post("/api/v1/product-comparison", json=selections)
    assert result.status_code == 409 and "same exact variant" in result.json()["detail"]


@pytest.mark.parametrize("state", ["stale", "future", "unavailable", "unpriced"])
def test_uncertain_quotes_stay_visible_but_cannot_win(client, db, selections, state):
    offer = db.get(Offer, "offer-256")
    if state == "stale":
        offer.current_observed_at = now() - timedelta(days=4)
    elif state == "future":
        offer.current_observed_at = now() + timedelta(hours=1)
    elif state == "unavailable":
        offer.current_in_stock = False
    else:
        offer.current_item_paise = None
    db.commit()
    column = client.post("/api/v1/product-comparison", json=selections).json()["products"][0]
    assert column["best_item_price_paise"] is None
    assert len(column["offers"]) == 1


def test_read_quota_is_used_without_lookup_or_retailer_activity(
    client, db, selections, monkeypatch
):
    monkeypatch.setattr(get_settings(), "read_requests_per_minute", 1)
    monkeypatch.setattr(
        "app.retail.fetch_public",
        lambda *args, **kwargs: pytest.fail("No retailer fetch permitted"),
    )
    assert client.post("/api/v1/product-comparison", json=selections).status_code == 200
    assert client.post("/api/v1/product-comparison", json=selections).status_code == 429


def test_group_offer_reads_are_batched_and_no_store_results_are_truncated(client, db, selections):
    for index in range(111):
        product_id = f"alias-{index}"
        db.add(
            Product(
                id=product_id,
                title="Alias",
                brand="Test",
                category="electronics",
                variant_label="128 GB",
                attributes={"storage_gb": 128},
                illustration="product",
            )
        )
        db.flush()
        db.add(
            Offer(
                id=f"alias-offer-{index}",
                product_id=product_id,
                retailer_id="shop",
                source_id="test",
                external_id=f"alias-external-{index}",
                seller="Merchant",
                current_item_paise=10000 + index,
                current_shipping_paise=0,
                current_in_stock=True,
                current_observed_at=now(),
            )
        )
        db.add(ProductMatch(left_id="phone-128", right_id=product_id, method="test", evidence={}))
    db.commit()
    queries = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        queries.append(statement)

    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        data = client.post("/api/v1/product-comparison", json=selections).json()
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)
    assert len(data["products"][1]["offers"]) == 112
    assert len([query for query in queries if "JOIN retailers" in query]) == 2


def test_demo_group_members_and_revoked_offers_are_never_returned(client, db, selections):
    demo = db.get(Product, "phone-256")
    demo.is_demo = True
    db.add(
        Product(
            id="third",
            title="Third",
            brand="Test",
            category="electronics",
            variant_label="Third",
            attributes={},
            illustration="product",
        )
    )
    db.flush()
    db.add(
        Offer(
            id="third-offer",
            product_id="third",
            retailer_id="shop",
            source_id="test",
            external_id="third",
            seller="Merchant",
            current_item_paise=30000,
            current_shipping_paise=0,
            current_in_stock=True,
            current_observed_at=now(),
        )
    )
    db.add(ProductMatch(left_id="phone-128", right_id="phone-256", method="test", evidence={}))
    db.commit()
    data = client.post(
        "/api/v1/product-comparison", json={"product_ids": ["phone-128", "third"]}
    ).json()
    assert [offer["id"] for offer in data["products"][0]["offers"]] == ["offer-128"]
