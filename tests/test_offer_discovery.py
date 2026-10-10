import json
from dataclasses import replace

import pytest

from app.comparison import scan_comparison
from app.config import Settings, get_settings
from app.lookup import store_listing
from app.retail import (
    Listing,
    discovery_retailers,
    parse_listing,
    product_rating,
    target_for,
)


def listing(retailer="flipkart", price=100000, **extras):
    urls = {
        "flipkart": "https://www.flipkart.com/contract/p/itm123?pid=MOBTEST128",
        "amazon": "https://www.amazon.in/dp/B012345678",
        "meesho": "https://www.meesho.com/contract/p/abc123",
        "croma": "https://www.croma.com/contract/p/321828",
        "reliance": "https://www.reliancedigital.in/product/contract-phone-9974691",
    }
    value = Listing(
        target_for(urls[retailer]),
        "Contract Phone Blue 128 GB 6 GB RAM",
        "Contract",
        "mobile",
        {"gtin13": "1234567890128", "color": "Blue"},
        None,
        price,
        0,
        "in_stock",
        "new",
        "Contract seller",
        {"value": 4.2, "best": 5, "count": 120},
    )
    return replace(value, **extras)


def test_registry_is_configurable_and_category_aware(monkeypatch):
    assert discovery_retailers() == ["amazon", "flipkart", "meesho", "croma", "reliance", "iqoo"]
    assert discovery_retailers("grocery") == ["amazon", "flipkart", "meesho"]
    monkeypatch.setattr(get_settings(), "discovery_retailers", ["croma", "amazon", "croma"])
    assert discovery_retailers("mobile") == ["croma", "amazon"]
    with pytest.raises(ValueError):
        Settings(_env_file=None, discovery_retailers=["unimplemented"])


@pytest.mark.parametrize(
    "url,retailer,identifier",
    [
        ("https://www.croma.com/phone/p/321828?tracking=1", "croma", "321828"),
        (
            "https://www.reliancedigital.in/product/nothing-phone-4a-9974691",
            "reliance",
            "nothing-phone-4a-9974691",
        ),
        ("https://www.reliancedigital.in/phone/p/493000001", "reliance", "493000001"),
    ],
)
def test_added_retailer_product_urls(url, retailer, identifier):
    target = target_for(url)
    assert target.retailer == retailer and target.external_id == identifier
    assert "tracking" not in target.url


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"ratingValue": True},
        {"ratingValue": "NaN"},
        {"ratingValue": "Infinity"},
        {"ratingValue": 6},
        {"ratingValue": 3, "bestRating": 0},
        {"ratingValue": 3, "bestRating": False},
    ],
)
def test_invalid_ratings_are_missing_instead_of_guessed(value):
    assert product_rating(value) is None


def test_rating_retains_reported_scale_and_optional_feedback_count():
    assert product_rating({"ratingValue": "8.4", "bestRating": 10, "reviewCount": "1,234"}) == {
        "value": 8.4,
        "best": 10.0,
        "count": 1234,
    }
    assert product_rating({"ratingValue": 4.2, "ratingCount": "1.2k"})["count"] is None


def test_structured_rating_belongs_to_the_exact_selected_product():
    target = target_for("https://www.flipkart.com/contract/p/itm123?pid=MOBTEST128")
    product = {
        "@type": "Product",
        "sku": "MOBTEST128",
        "name": "Contract Phone Blue 128 GB 6 GB RAM",
        "brand": {"name": "Contract"},
        "image": "https://rukminim2.flixcart.com/contract.jpg",
        "aggregateRating": {"ratingValue": 4.3, "ratingCount": 120},
        "offers": {
            "@type": "Offer",
            "price": 1000,
            "priceCurrency": "INR",
            "availability": "https://schema.org/InStock",
            "itemCondition": "https://schema.org/NewCondition",
        },
    }
    other = {
        **product,
        "sku": "MOBOTHER128",
        "aggregateRating": {"ratingValue": 1, "ratingCount": 5},
    }
    html = '<script type="application/ld+json">' + json.dumps([product, other]) + "</script>"
    parsed = parse_listing(html, target)
    assert parsed.rating == {"value": 4.3, "best": 5.0, "count": 120}
    assert parsed.image_url == product["image"]


def test_one_lookup_comparison_returns_four_other_verified_stores(client, db, monkeypatch):
    anchor = listing()
    product_id = store_listing(db, anchor)

    def discover(retailer, query, expected=None):
        candidate = listing(retailer, price=90000 if retailer == "amazon" else 110000)
        return {
            "search": {"status": "ok", "search_url": candidate.target.url},
            "listings": [candidate],
            "failures": [],
        }

    monkeypatch.setattr("app.comparison.discover_store", discover)
    result = client.post(f"/api/v1/products/{product_id}/compare")
    assert result.status_code == 200
    assert len(result.json()["stores"]) == 4 and all(
        store["status"] == "verified" for store in result.json()["stores"]
    )
    detail = client.get(f"/api/v1/products/{product_id}").json()
    assert {offer["retailer_id"] for offer in detail["offers"]} == {
        "amazon",
        "flipkart",
        "meesho",
        "croma",
        "reliance",
    }
    assert detail["best_item_price_paise"] == 90000
    for offer in detail["offers"]:
        assert (
            offer["title"] == anchor.title
            and offer["rating"]["value"] == 4.2
            and offer["rating"]["observed_at"]
        )


def test_tracked_generic_domains_join_without_a_search_adapter(client, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "discovery_retailers", [])
    first = listing(target=target_for("https://first-retailer.org/product/phone"))
    second = listing(target=target_for("https://second-retailer.org/product/phone"), price=95000)
    anchor_id = store_listing(db, first)
    second_id = store_listing(db, second)
    result = scan_comparison(db, anchor_id)
    assert not result["stores"]
    offers = client.get(f"/api/v1/products/{anchor_id}").json()["offers"]
    assert {offer["product_id"] for offer in offers} == {anchor_id, second_id}
    assert len(client.get(f"/api/v1/products/{anchor_id}/history").json()["series"]) == 2


def test_wrong_variant_and_wrong_retailer_cannot_join_verified_cards(client, db, monkeypatch):
    anchor_id = store_listing(db, listing())

    def discover(retailer, query, expected=None):
        candidate = listing(
            "amazon", attributes={"gtin13": "1234567890128", "color": "Red"}, price=100
        )
        return {
            "search": {"status": "ok", "search_url": candidate.target.url},
            "listings": [candidate],
            "failures": [],
        }

    monkeypatch.setattr("app.comparison.discover_store", discover)
    result = scan_comparison(db, anchor_id)
    assert result["stores"][0]["items"][0]["match_status"] == "different_variant"
    assert all(not store["items"] for store in result["stores"][1:])
    assert len(client.get(f"/api/v1/products/{anchor_id}").json()["offers"]) == 1


def test_missing_rating_clears_the_previous_value_on_a_new_real_check(client, db):
    candidate = listing()
    product_id = store_listing(db, candidate)
    assert client.get(f"/api/v1/products/{product_id}").json()["offers"][0]["rating"]
    store_listing(
        db,
        replace(
            candidate,
            rating=None,
            title="Updated product heading",
            image_url="https://rukminim2.flixcart.com/updated.jpg",
        ),
    )
    offer = client.get(f"/api/v1/products/{product_id}").json()["offers"][0]
    assert offer["rating"] is None
    assert offer["title"] == "Updated product heading"
    assert offer["image_url"].endswith("/updated.jpg")


def test_cached_scan_can_join_a_newly_tracked_generic_store(client, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "discovery_retailers", [])
    first = listing(target=target_for("https://first-retailer.org/product/phone"))
    product_id = store_listing(db, first)
    assert not scan_comparison(db, product_id)["cached"]
    second = listing(target=target_for("https://second-retailer.org/product/phone"))
    store_listing(db, second)
    assert scan_comparison(db, product_id)["cached"]
    assert len(client.get(f"/api/v1/products/{product_id}").json()["offers"]) == 2


def test_revoked_generic_store_cannot_join_catalog_matches(client, db, monkeypatch):
    from app.models import Source

    monkeypatch.setattr(get_settings(), "discovery_retailers", [])
    first = listing(target=target_for("https://first-retailer.org/product/phone"))
    product_id = store_listing(db, first)
    second = listing(target=target_for("https://second-retailer.org/product/phone"))
    store_listing(db, second)
    source = db.get(Source, "public-page:" + second.target.retailer)
    source.display_allowed = False
    db.commit()
    scan_comparison(db, product_id)
    assert len(client.get(f"/api/v1/products/{product_id}").json()["offers"]) == 1
