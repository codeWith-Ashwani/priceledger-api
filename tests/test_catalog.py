from datetime import timedelta

from app.db import now
from app.models import Offer, Source


def test_catalog_paginates_and_respects_exact_variants(client):
    response = client.get("/api/v1/products", params={"limit": 1})
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert len(response.json()["items"]) == 1
    assert client.get("/api/v1/products/phone-128").json()["best_price_paise"] == 10_500
    assert client.get("/api/v1/products/phone-256").json()["offers"] == []


def test_search_escapes_wildcards_and_filters_category(client):
    assert client.get("/api/v1/products", params={"q": "%"}).json()["total"] == 0
    assert (
        client.get("/api/v1/products", params={"q": "test", "category": "electronics"}).json()[
            "total"
        ]
        == 2
    )
    assert client.get("/api/v1/products", params={"category": "fashion"}).json()["total"] == 0


def test_unknown_shipping_cannot_be_a_cheapest_total(client, db):
    db.get(Offer, "offer-128").current_shipping_paise = None
    db.commit()
    offer = client.get("/api/v1/products/phone-128").json()
    assert offer["best_price_paise"] is None
    assert offer["offers"][0]["item_paise"] == 10_000


def test_stale_and_unavailable_offers_are_excluded(client, db):
    offer = db.get(Offer, "offer-128")
    offer.current_observed_at = now() - timedelta(days=4)
    db.commit()
    detail = client.get("/api/v1/products/phone-128").json()
    assert detail["best_price_paise"] is None
    assert detail["offers"][0]["stale"] is True
    offer.current_observed_at = now()
    offer.current_in_stock = False
    db.commit()
    assert client.get("/api/v1/products/phone-128").json()["best_price_paise"] is None


def test_revoked_source_is_not_displayed(client, db):
    db.get(Source, "test").display_allowed = False
    db.commit()
    assert client.get("/api/v1/products/phone-128").json()["offers"] == []


def test_query_bounds_and_unknown_product(client):
    assert client.get("/api/v1/products", params={"limit": 101}).status_code == 422
    assert client.get("/api/v1/products/missing").status_code == 404
    assert client.get("/api/v1/products/phone-128/history", params={"days": 0}).status_code == 422
