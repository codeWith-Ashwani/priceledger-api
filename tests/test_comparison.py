from dataclasses import replace

import pytest
from sqlalchemy import func, select

from app.comparison import scan_comparison
from app.identity import match_variant, mobile_attributes, valid_gtin
from app.lookup import store_listing
from app.models import Observation, ProductMatch, Source
from app.retail import Listing, Target


@pytest.fixture
def pair():
    common = {
        "model": "Phone 4a",
        "color": "Blue",
        "ram_gb": 6,
        "storage_gb": 128,
        "identity_category": "mobile",
    }
    left = Listing(
        Target("flipkart", "https://www.flipkart.com/test/p/itm123?pid=MOBTEST128", "MOBTEST128"),
        "Test Phone 4a Blue 128 GB 6 GB RAM",
        "Test",
        "mobile",
        common,
        None,
        3699900,
        0,
        "in_stock",
        "new",
        "Test seller",
    )
    right = replace(
        left,
        target=Target("amazon", "https://www.amazon.in/dp/B012345678", "B012345678"),
        item_paise=3499900,
        attributes={**common, "sku": "other-store-id"},
    )
    return left, right


def discovery(listing):
    return {
        "search": {"status": "ok", "search_url": "https://www.amazon.in/s?k=test"},
        "listings": [listing],
        "failures": [],
    }


def test_matching_phone_requires_same_ram_color_storage_and_condition(pair):
    left, right = pair
    assert match_variant(left, right)["status"] == "verified"
    for key, value in [
        ("ram_gb", 8),
        ("storage_gb", 256),
        ("color", "White"),
        ("model", "Phone 4a Pro"),
    ]:
        other = replace(right, attributes={**right.attributes, key: value})
        assert match_variant(left, other)["status"] == "different_variant"
    assert match_variant(left, replace(right, condition="used"))["status"] == "different_variant"
    assert match_variant(left, replace(right, condition="unknown"))["status"] == "review"


def test_gtin_supports_all_categories_but_rejects_invalid_barcodes(pair):
    left, right = pair
    assert valid_gtin("1234567890128") == "01234567890128"
    assert valid_gtin("1234567890129") is None
    a = replace(left, category="grocery", attributes={"gtin13": "1234567890128", "size": "500g"})
    b = replace(right, category="grocery", attributes={"gtin14": "01234567890128", "size": "500g"})
    assert match_variant(a, b)["method"] == "gtin"
    assert (
        match_variant(a, replace(b, attributes={**b.attributes, "size": "1kg"}))["status"]
        == "different_variant"
    )


def test_ram_and_storage_are_extracted_from_the_selected_title():
    attributes = mobile_attributes(
        "Nothing Phone (4a) (Blue, 128 GB) (6 GB RAM)", "Nothing", {"color": "Blue"}
    )
    assert attributes["ram_gb"] == 6
    assert attributes["storage_gb"] == 128
    assert attributes["model"] == "Phone (4a)"
    assert "identity_category" not in mobile_attributes(
        "Phone case blue 128 GB 6 GB RAM", "Test", {}
    )


def test_one_link_discovery_joins_prices_and_store_histories(client, db, monkeypatch, pair):
    left, right = pair
    product_id = store_listing(db, left)
    monkeypatch.setattr(
        "app.comparison.discover_store",
        lambda retailer, query, expected=None: (
            discovery(right)
            if retailer == "amazon"
            else {
                "search": {
                    "status": "no_matches",
                    "search_url": "https://www.meesho.com/search?q=test",
                },
                "listings": [],
                "failures": [],
            }
        ),
    )
    response = client.post(f"/api/v1/products/{product_id}/compare")
    assert response.status_code == 200
    assert response.json()["stores"][0]["status"] == "verified"
    detail = client.get(f"/api/v1/products/{product_id}").json()
    assert len(detail["offers"]) == 2
    assert detail["best_price_paise"] == 3499900
    history = client.get(f"/api/v1/products/{product_id}/history?basis=item").json()
    assert {series["name"] for series in history["series"]} == {"Amazon", "Flipkart"}
    assert all(series["source_name"] and series["product_url"] for series in history["series"])
    assert client.post(f"/api/v1/products/{product_id}/compare").json()["cached"]
    assert db.scalar(select(func.count()).select_from(Observation)) == 2


def test_uncertain_matches_never_enter_cheapest_price_or_history(client, db, monkeypatch, pair):
    left, right = pair
    product_id = store_listing(db, left)
    right = replace(right, condition="unknown", item_paise=100)
    monkeypatch.setattr(
        "app.comparison.discover_store", lambda retailer, query, expected=None: discovery(right)
    )
    result = scan_comparison(db, product_id)
    assert result["stores"][0]["items"][0]["match_status"] == "review"
    assert db.scalar(select(func.count()).select_from(ProductMatch)) == 0
    assert len(client.get(f"/api/v1/products/{product_id}").json()["offers"]) == 1
    assert client.get(f"/api/v1/products/{product_id}").json()["best_price_paise"] == 3699900


def test_revoked_source_is_removed_from_cached_comparison_and_history(
    client, db, monkeypatch, pair
):
    left, right = pair
    product_id = store_listing(db, left)
    monkeypatch.setattr(
        "app.comparison.discover_store", lambda retailer, query, expected=None: discovery(right)
    )
    scan_comparison(db, product_id)
    db.get(Source, "public-page:amazon").display_allowed = False
    db.commit()
    result = scan_comparison(db, product_id)
    assert not result["cached"]
    assert not result["stores"][0]["items"]
    assert len(client.get(f"/api/v1/products/{product_id}/history").json()["series"]) == 1
