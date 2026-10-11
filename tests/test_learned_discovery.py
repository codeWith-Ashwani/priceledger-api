import json
from dataclasses import replace
from datetime import timedelta

import pytest

from app.config import get_settings
from app.db import now
from app.discovery import learn_store, registered_stores
from app.lookup import store_listing
from app.models import DiscoveryStore, Source
from app.retail import parse_listing, search_store, target_for


def product_page(sku="TEST520", handle="test-headphones", price=1000):
    # Isolated parser fixtures, never inserted into the application's catalog.
    structured = {
        "@type": "Product",
        "name": "Test WH-520 Headphones",
        "sku": sku,
        "brand": {"name": "Test"},
        "offers": {
            "@type": "Offer",
            "price": price,
            "priceCurrency": "INR",
            "availability": "https://schema.org/InStock",
            "itemCondition": "https://schema.org/NewCondition",
        },
    }
    catalog = {"handle": handle, "variants": [{"id": 123, "sku": "TEST520"}]}
    return (
        '<script type="application/ld+json">'
        + json.dumps(structured)
        + '</script><script type="application/json" id="ProductJson-template">'
        + json.dumps(catalog)
        + "</script>"
    )


def listing():
    return parse_listing(
        product_page(), target_for("https://shop.example.org/products/test-headphones?variant=123")
    )


def test_learning_requires_matching_literal_handle_and_sku():
    target = listing().target
    assert listing().discovery_kind == "shopify_html"
    assert parse_listing(product_page(sku="OTHER"), target).discovery_kind is None
    assert parse_listing(product_page(handle="another-product"), target).discovery_kind is None
    assert (
        parse_listing(product_page(), target_for(target.url.replace("123", "999"))).discovery_kind
        is None
    )


def test_store_is_brand_scoped_and_respects_pause_permissions_expiry_and_limit(db, monkeypatch):
    value = listing()
    store_listing(db, value)
    assert value.target.retailer in registered_stores(db, "test")
    assert not registered_stores(db, "Another brand")
    row = db.get(DiscoveryStore, value.target.retailer)
    row.enabled = False
    db.commit()
    learn_store(db, replace(value, brand="Another brand"))
    db.commit()
    assert not row.enabled and row.brands == ["Test"]
    assert not registered_stores(db)
    row.enabled = True
    source = db.get(Source, row.source_id)
    source.display_allowed = False
    db.commit()
    assert not registered_stores(db)
    source.display_allowed = True
    source.expires_at = now() - timedelta(seconds=1)
    db.commit()
    assert not registered_stores(db)
    source.expires_at = None
    db.commit()
    monkeypatch.setattr(get_settings(), "discovery_extra_store_limit", 0)
    assert not registered_stores(db)


def test_non_shopify_and_missing_brands_never_learn(db):
    for value in (
        replace(listing(), discovery_kind=None),
        replace(listing(), brand="Brand not stated"),
    ):
        store_listing(db, value)
    assert not registered_stores(db)


def test_configured_learning_capacity_never_hides_already_registered_stores(db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "discovery_registry_capacity", 2)
    monkeypatch.setattr(settings, "discovery_brands_per_store", 1)
    first = listing()
    second = replace(
        first, target=target_for(first.target.url.replace("shop.example.org", "second.example.org"))
    )
    third = replace(
        first, target=target_for(first.target.url.replace("shop.example.org", "third.example.org"))
    )
    store_listing(db, first)
    store_listing(db, second)
    monkeypatch.setattr(settings, "discovery_registry_capacity", 1)
    store_listing(db, third)
    learn_store(db, replace(first, brand="Another brand"))
    db.commit()
    assert set(registered_stores(db)) == {first.target.retailer, second.target.retailer}
    assert db.get(DiscoveryStore, first.target.retailer).brands == ["Test"]


def test_persisted_host_is_revalidated(db):
    value = listing()
    store_listing(db, value)
    db.get(DiscoveryStore, value.target.retailer).host = "127.0.0.1"
    db.commit()
    assert not registered_stores(db)


def test_search_keeps_same_store_product_candidates_and_does_not_trust_search_prices(monkeypatch):
    calls = []
    html = """<a href="/products/test-headphones">Test WH-520 Headphones ₹1</a>
    <a href="https://other.example.org/products/test-headphones">Test WH-520 Headphones</a>
    <a href="/collections/test">Test WH-520 Headphones</a>"""
    monkeypatch.setattr(
        "app.retail.fetch_page", lambda url, retailer: (calls.append(url) or html, url)
    )
    value = listing()
    result = search_store(
        value.target.retailer,
        "Test WH-520",
        custom={"name": "shop.example.org", "search_base": "https://shop.example.org/search?"},
    )
    assert len(result["items"]) == 1
    assert "type=product" in calls[0]
    assert "item_paise" not in result["items"][0]
    assert result["items"][0]["url"].startswith("https://shop.example.org/products/")


def test_name_lookup_searches_learned_store(db, client, monkeypatch):
    value = listing()
    store_listing(db, value)
    monkeypatch.setattr("app.lookup.discovery_retailers", lambda: [])
    calls = []

    def search(retailer, query, *, custom):
        calls.append((retailer, custom))
        return {
            "retailer": retailer,
            "retailer_name": custom["name"],
            "status": "no_matches",
            "message": "No matches",
            "search_url": custom["search_base"],
            "items": [],
        }

    monkeypatch.setattr("app.lookup.search_store", search)
    result = client.post("/api/v1/lookup", json={"query": "Test WH-520"})
    assert result.status_code == 200
    assert len(calls) == 1 and calls[0][0] == value.target.retailer


def test_admin_controls_require_auth_and_do_not_reactivate_source(db, client, monkeypatch):
    store_listing(db, listing())
    path = "/api/v1/admin/discovery-stores"
    monkeypatch.setattr(get_settings(), "admin_token", "discovery-test-secret")
    assert client.get(path).status_code == 401
    headers = {"X-Admin-Token": "discovery-test-secret"}
    response = client.get(path, headers=headers)
    assert response.headers["Cache-Control"] == "no-store"
    retailer = response.json()[0]["retailer_id"]
    assert (
        client.post(
            path + f"/{retailer}/status", headers=headers, json={"enabled": "false"}
        ).status_code
        == 422
    )
    assert (
        client.post(path + "/missing/status", headers=headers, json={"enabled": False}).status_code
        == 404
    )
    assert (
        client.post(
            path + f"/{retailer}/status", headers=headers, json={"enabled": False}
        ).status_code
        == 200
    )
    assert not registered_stores(db)
    db.get(Source, "public-page:" + retailer).history_allowed = False
    db.commit()
    client.post(path + f"/{retailer}/status", headers=headers, json={"enabled": True})
    assert not registered_stores(db)


def test_comparison_searches_observed_brand_store_and_pause_invalidates_cache(db, monkeypatch):
    from app.comparison import scan_comparison

    learned = listing()
    store_listing(db, learned)
    anchor = replace(
        learned, target=target_for("https://www.amazon.in/dp/B012345678"), discovery_kind=None
    )
    product_id = store_listing(db, anchor)
    monkeypatch.setattr("app.comparison.discovery_retailers", lambda *args: [])
    calls = []

    def discover(retailer, query, expected, *, custom):
        calls.append(retailer)
        return {
            "search": {"status": "ok", "search_url": custom["search_base"]},
            "listings": [learned],
            "failures": [],
        }

    monkeypatch.setattr("app.comparison.discover_store", discover)
    first = scan_comparison(db, product_id)
    assert calls == [learned.target.retailer]
    assert first["stores"][0]["retailer"] == learned.target.retailer
    assert scan_comparison(db, product_id)["cached"]
    db.get(DiscoveryStore, learned.target.retailer).enabled = False
    db.commit()
    assert not scan_comparison(db, product_id)["cached"]
    assert calls == [learned.target.retailer]


@pytest.mark.parametrize("brand", ["", "Brand not stated"])
def test_unbranded_store_is_not_registered(db, brand):
    store_listing(db, replace(listing(), brand=brand))
    assert not registered_stores(db)
