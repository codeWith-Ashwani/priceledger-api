import json
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select

import app.lookup as lookup_module
from app.config import get_settings
from app.db import now
from app.models import Observation, Offer, Product, Source
from app.retail import (
    Listing,
    LookupError,
    Target,
    fetch_page,
    parse_listing,
    search_store,
    target_for,
)


@pytest.fixture
def listing():
    # Controlled test fixture; never seeded into the application catalog.
    return Listing(
        Target(
            "flipkart", "https://www.flipkart.com/test-phone/p/itm123?pid=MOBTEST123", "MOBTEST123"
        ),
        "Test Phone (Blue, 128 GB)",
        "Test",
        "mobile",
        {"sku": "MOBTEST123", "color": "Blue"},
        None,
        36_999_00,
        0,
        "in_stock",
        "new",
        "Seller not stated",
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private",
        "https://www.flipkart.com.evil.test/p/itm123",
        "https://www.flipkart.com@127.0.0.1/p/itm123",
        "https://www.flipkart.com:8080/p/itm123",
        "file:///etc/passwd",
        "https://www.amazon.in/s?k=phone",
    ],
)
def test_rejects_unsafe_and_non_product_links(url):
    with pytest.raises(LookupError):
        target_for(url)


def test_flipkart_url_preserves_variant_pid_and_discards_tracking():
    target = target_for(
        "https://www.flipkart.com/phone/p/itm123?pid=MOBTEST123&lid=tracking&marketplace=FLIPKART#reviews"
    )
    assert target.external_id == "MOBTEST123"
    assert target.url == "https://www.flipkart.com/phone/p/itm123?pid=MOBTEST123"


def page(price=36999, currency="INR", sku="MOBTEST123", shipping=True):
    offer = {
        "@type": "Offer",
        "price": price,
        "priceCurrency": currency,
        "availability": "https://schema.org/InStock",
        "itemCondition": "https://schema.org/NewCondition",
    }
    if shipping:
        offer["shippingDetails"] = {
            "shippingRate": {"value": 0, "currency": "INR"},
            "shippingDestination": {"addressCountry": "IN"},
        }
    return (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Test Phone (Blue, 128 GB)",
                "sku": sku,
                "brand": {"name": "Test"},
                "offers": offer,
            }
        )
        + "</script>"
    )


def test_structured_price_paise_and_unknown_shipping(listing):
    result = parse_listing(page(price="1234.50", shipping=False), listing.target)
    assert result.item_paise == 123450
    assert result.shipping_paise is None
    assert result.availability == "in_stock"


@pytest.mark.parametrize(
    "changes",
    [
        {"currency": "USD"},
        {"sku": "WRONGVARIANT"},
        {"price": "NaN"},
        {"price": -1},
        {"price": "1.001"},
    ],
)
def test_wrong_variant_currency_and_invalid_prices_are_rejected(listing, changes):
    with pytest.raises(LookupError):
        parse_listing(page(**changes), listing.target)


def test_captcha_does_not_create_a_price(listing):
    with pytest.raises(LookupError, match="CAPTCHA"):
        parse_listing('<input id="captchacharacters">' + page(), listing.target)


def test_amazon_visible_price_to_pay_never_uses_mrp():
    target = target_for("https://www.amazon.in/dp/B012345678")
    html = """<span id="productTitle">Test Phone Blue 128 GB</span><input id="ASIN" value="B012345678">
    <a id="bylineInfo">Visit the Test Store</a><div id="availability">In stock</div>
    <div id="corePriceDisplay_desktop_feature_div"><span class="priceToPay a-price"><span class="a-offscreen"> </span><span class="a-price-symbol">₹</span><span class="a-price-whole">40,990</span></span><span class="a-price a-text-price"><span class="a-offscreen">₹62,999</span></span></div>"""
    result = parse_listing(html, target)
    assert result.item_paise == 4099000
    assert result.brand == "Test"
    assert result.shipping_paise is None


def test_flipkart_link_without_pid_is_identified_by_actual_sku(listing):
    target = target_for("https://www.flipkart.com/test-phone/p/itm123")
    result = parse_listing(page(), target)
    assert result.target.external_id == "MOBTEST123"
    assert result.target.url.endswith("?pid=MOBTEST123")


def test_lookup_persists_real_contract_and_deduplicates_recent_requests(
    client, db, monkeypatch, listing
):
    calls = []
    monkeypatch.setattr(
        lookup_module, "load_listing", lambda target: calls.append(target) or listing
    )
    result = client.post("/api/v1/lookup", json={"query": listing.target.url})
    assert result.status_code == 200
    product_id = result.json()["product_id"]
    detail = client.get(f"/api/v1/products/{product_id}").json()
    assert detail["title"] == listing.title
    assert detail["best_price_paise"] == 3699900
    assert detail["offers"][0]["channel"] == "online"
    assert detail["offers"][0]["product_url"] == listing.target.url
    assert client.post("/api/v1/lookup", json={"query": listing.target.url}).json()["cached"]
    assert len(calls) == 1
    assert db.scalar(select(func.count()).select_from(Observation)) == 1
    history = client.get(f"/api/v1/products/{product_id}/history?basis=item").json()
    assert len(history["series"][0]["points"]) == 1
    assert history["series"][0]["points"][0]["price_paise"] == 3699900
    assert detail["launch_date"] is None


def test_future_checks_append_without_fabricating_backfill(client, db, monkeypatch, listing):
    monkeypatch.setattr(lookup_module, "load_listing", lambda target: listing)
    product_id = client.post("/api/v1/lookup", json={"query": listing.target.url}).json()[
        "product_id"
    ]
    offer = db.get(Offer, product_id + ":public-page")
    offer.current_observed_at = now() - timedelta(hours=1)
    db.commit()
    monkeypatch.setattr(
        lookup_module, "load_listing", lambda target: replace(listing, item_paise=3500000)
    )
    assert client.post("/api/v1/lookup", json={"query": listing.target.url}).status_code == 200
    assert db.scalar(select(func.count()).select_from(Observation)) == 2
    assert offer.current_item_paise == 3500000


def test_unknown_shipping_is_not_a_free_total_but_item_history_is_visible(
    client, monkeypatch, listing
):
    monkeypatch.setattr(
        lookup_module, "load_listing", lambda target: replace(listing, shipping_paise=None)
    )
    product_id = client.post("/api/v1/lookup", json={"query": listing.target.url}).json()[
        "product_id"
    ]
    assert client.get(f"/api/v1/products/{product_id}").json()["best_price_paise"] is None
    assert (
        client.get(f"/api/v1/products/{product_id}/history?basis=item").json()["series"][0][
            "points"
        ][0]["price_paise"]
        == 3699900
    )
    assert (
        client.get(f"/api/v1/products/{product_id}/history").json()["series"][0]["points"][0][
            "price_paise"
        ]
        is None
    )


def test_blocked_retailer_records_nothing(client, db, monkeypatch, listing):
    def blocked(target):
        raise LookupError("retailer_blocked", "Retailer refused access")

    monkeypatch.setattr(lookup_module, "load_listing", blocked)
    result = client.post("/api/v1/lookup", json={"query": listing.target.url})
    assert result.status_code == 502
    assert result.json()["detail"]["code"] == "retailer_blocked"
    assert db.scalar(select(func.count()).select_from(Observation)) == 0


def test_demo_products_never_appear_even_if_fixture_mode_enabled(client, db, monkeypatch):
    db.get(Product, "phone-128").is_demo = True
    db.commit()
    monkeypatch.setattr(get_settings(), "demo_mode", True)
    assert client.get("/api/v1/products/phone-128").status_code == 404
    assert client.get("/api/v1/products").json()["total"] == 1


def test_source_revocation_applies_to_cached_lookup(client, db, monkeypatch, listing):
    monkeypatch.setattr(lookup_module, "load_listing", lambda target: listing)
    client.post("/api/v1/lookup", json={"query": listing.target.url})
    db.get(Source, "public-page:flipkart").history_allowed = False
    db.commit()
    assert client.post("/api/v1/lookup", json={"query": listing.target.url}).status_code == 403
    assert db.scalar(select(func.count()).select_from(Observation)) == 1


def test_variant_change_is_not_appended(client, db, monkeypatch, listing):
    monkeypatch.setattr(lookup_module, "load_listing", lambda target: listing)
    product_id = client.post("/api/v1/lookup", json={"query": listing.target.url}).json()[
        "product_id"
    ]
    db.get(Offer, product_id + ":public-page").current_observed_at = now() - timedelta(hours=1)
    db.commit()
    monkeypatch.setattr(
        lookup_module,
        "load_listing",
        lambda target: replace(listing, attributes={"sku": "MOBTEST123", "color": "Red"}),
    )
    assert client.post("/api/v1/lookup", json={"query": listing.target.url}).status_code == 409
    assert db.scalar(select(func.count()).select_from(Observation)) == 1


def test_lookup_rate_limit(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "lookup_requests_per_minute", 1)
    assert (
        client.post("/api/v1/lookup", json={"query": "https://evil.test/p/123"}).status_code == 400
    )
    result = client.post("/api/v1/lookup", json={"query": "https://evil.test/p/123"})
    assert result.status_code == 429
    assert result.headers["Retry-After"] == "60"


def test_redirect_cannot_escape_retailer_allowlist(monkeypatch):
    class Response:
        status = 302

        def getheader(self, name):
            return "http://127.0.0.1/private" if name == "Location" else None

    class Connection:
        def __init__(self, *args):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr("app.public_http.public_addresses", lambda host: ["93.184.216.34"])
    monkeypatch.setattr("app.public_http.PinnedHTTPSConnection", Connection)
    with pytest.raises(LookupError, match="product link"):
        fetch_page("https://www.flipkart.com/p/itm123", "flipkart")


def test_brand_model_search_returns_unpriced_exact_candidates(monkeypatch):
    html = '<a title="Test Phone 128 GB Blue" href="/test-phone/p/itm123?pid=MOBTEST123">Test Phone 128 GB Blue</a><a href="/wrong/p/itm456">Different Phone</a>'
    monkeypatch.setattr("app.retail.fetch_page", lambda url, retailer: (html, url))
    result = search_store("flipkart", "Test Phone")
    assert len(result["items"]) == 1
    assert "price" not in result["items"][0]
    assert "Choose the exact" in result["message"]
