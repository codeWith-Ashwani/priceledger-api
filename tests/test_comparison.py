from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.comparison import discover_store, scan_comparison
from app.config import get_settings
from app.db import now
from app.identity import match_variant, mobile_attributes, valid_gtin
from app.lookup import store_listing
from app.models import CollectionAttempt, ComparisonScan, Observation, ProductMatch, Source
from app.retail import Listing, LookupError, Target


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


@pytest.mark.parametrize("maximum", [1, 3])
def test_candidate_verification_reads_only_the_configured_number(monkeypatch, pair, maximum):
    _, listing = pair
    monkeypatch.setattr(get_settings(), "discovery_candidates_per_store", maximum)
    urls = [f"https://www.amazon.in/dp/B01234567{index}" for index in range(4)]
    monkeypatch.setattr(
        "app.comparison.search_store",
        lambda *args, **kwargs: {
            "status": "ok",
            "items": [{"title": "Contract phone", "url": url} for url in urls],
        },
    )
    fetched = []

    def load(target):
        fetched.append(target.url)
        return listing

    monkeypatch.setattr("app.comparison.load_listing", load)
    assert len(discover_store("amazon", "Contract phone")["listings"]) == maximum
    assert len(fetched) == maximum


def test_candidate_configuration_updates_reservations_and_invalidates_cached_scans(
    db, monkeypatch, pair
):
    left, right = pair
    product_id = store_listing(db, left)
    settings = get_settings()
    monkeypatch.setattr(settings, "discovery_retailers", ["amazon"])
    monkeypatch.setattr(settings, "discovery_candidates_per_store", 1)
    costs = []

    def reserve(_db, retailers, *, cost, custom):
        costs.append(cost)
        return retailers, {}

    monkeypatch.setattr("app.comparison.reserve_stores", reserve)
    monkeypatch.setattr("app.comparison.discover_store", lambda *args, **kwargs: discovery(right))
    assert not scan_comparison(db, product_id)["cached"]
    assert scan_comparison(db, product_id)["cached"]
    monkeypatch.setattr(settings, "discovery_candidates_per_store", 4)
    assert not scan_comparison(db, product_id)["cached"]
    assert costs == [2, 5]


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
    assert result["stores"][0]["items"][0]["match_status"] == "model_match"
    assert result["model_offers"][0]["condition"] == "unknown"
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


def test_rejected_search_candidates_do_not_break_other_store_results(client, db, monkeypatch, pair):
    left, right = pair
    product_id = store_listing(db, left)
    monkeypatch.setattr(get_settings(), "discovery_retailers", ["amazon", "meesho"])
    monkeypatch.setattr(
        "app.comparison.search_store",
        lambda retailer, query: {
            "status": "ok",
            "search_url": "https://www.amazon.in/s?k=contract",
            "items": [{"title": right.title, "url": "https://www.amazon.in/s?k=invalid"}]
            if retailer == "amazon"
            else [],
        },
    )
    monkeypatch.setattr("app.comparison.load_listing", lambda _: pytest.fail("Invalid URL fetched"))
    response = client.post(f"/api/v1/products/{product_id}/compare")
    assert response.status_code == 200
    assert len(response.json()["stores"]) == 2
    assert "supported product page" in response.json()["stores"][0]["message"]
    assert db.scalar(select(func.count()).select_from(Observation)) == 1


def test_cross_store_search_candidate_is_rejected_before_fetch(monkeypatch, pair):
    left, _ = pair
    monkeypatch.setattr(
        "app.comparison.search_store",
        lambda *args: {"status": "ok", "items": [{"title": left.title, "url": left.target.url}]},
    )
    monkeypatch.setattr(
        "app.comparison.load_listing", lambda _: pytest.fail("Unreserved source fetched")
    )
    result = discover_store("amazon", "Test phone")
    assert result["listings"] == result["failed_checks"] == []
    assert "another retailer" in result["failures"][0]


def test_duplicates_and_rejected_urls_do_not_consume_valid_candidate_slots(monkeypatch, pair):
    _, right = pair
    monkeypatch.setattr(get_settings(), "discovery_candidates_per_store", 2)
    another_url = "https://www.amazon.in/dp/B012345679"
    monkeypatch.setattr(
        "app.comparison.search_store",
        lambda *args: {
            "status": "ok",
            "items": [
                {"title": right.title, "url": url}
                for url in [
                    "https://www.amazon.in/s?k=bad",
                    right.target.url,
                    right.target.url,
                    another_url,
                ]
            ],
        },
    )
    calls = []

    def load(target):
        calls.append(target.url)
        return replace(right, target=target)

    monkeypatch.setattr("app.comparison.load_listing", load)
    assert len(discover_store("amazon", "Test phone")["listings"]) == 2
    assert calls == [right.target.url, another_url]


def test_failed_candidate_retains_validated_target_and_is_recorded_once(
    client, db, monkeypatch, pair
):
    left, right = pair
    product_id = store_listing(db, left)
    monkeypatch.setattr(get_settings(), "discovery_retailers", ["amazon"])
    monkeypatch.setattr(
        "app.comparison.search_store",
        lambda *args: {
            "status": "ok",
            "search_url": "https://www.amazon.in/s?k=contract",
            "items": [{"title": right.title, "url": right.target.url}] * 2,
        },
    )
    calls = []

    def fail(target):
        calls.append(target)
        raise LookupError("retailer_unreadable", "This product page cannot currently be read.")

    monkeypatch.setattr("app.comparison.load_listing", fail)
    response = client.post(f"/api/v1/products/{product_id}/compare")
    assert response.status_code == 200
    assert len(calls) == 1
    failures = list(
        db.scalars(select(CollectionAttempt).where(CollectionAttempt.outcome == "failure"))
    )
    assert len(failures) == 1
    assert failures[0].product_url == right.target.url
    assert failures[0].error_code == "retailer_unreadable"


def test_future_scan_timestamp_does_not_qualify_for_cache(db, monkeypatch, pair):
    left, right = pair
    product_id = store_listing(db, left)
    monkeypatch.setattr(get_settings(), "discovery_retailers", ["amazon"])
    calls = []
    monkeypatch.setattr(
        "app.comparison.discover_store",
        lambda *args: calls.append(True) or discovery(right),
    )
    scan_comparison(db, product_id)
    db.get(ComparisonScan, product_id).checked_at = now() + timedelta(hours=1)
    db.commit()
    assert not scan_comparison(db, product_id)["cached"]
    assert len(calls) == 2


@pytest.mark.parametrize("permission", ["display_allowed", "history_allowed", "expired", "demo"])
def test_cached_comparison_cannot_use_disabled_anchor(client, db, monkeypatch, pair, permission):
    left, right = pair
    product_id = store_listing(db, left)
    monkeypatch.setattr(get_settings(), "discovery_retailers", ["amazon"])
    monkeypatch.setattr("app.comparison.discover_store", lambda *args: discovery(right))
    scan_comparison(db, product_id)
    source = db.get(Source, "public-page:flipkart")
    if permission == "expired":
        source.expires_at = now() - timedelta(seconds=1)
    elif permission == "demo":
        source.kind = "demo"
    else:
        setattr(source, permission, False)
    db.commit()
    monkeypatch.setattr(
        "app.comparison.discover_store", lambda *args: pytest.fail("Disabled source used")
    )
    assert client.post(f"/api/v1/products/{product_id}/compare").status_code == 403
