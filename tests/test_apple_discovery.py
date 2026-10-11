"""Isolated contracts for automatic reseller discovery; no live catalog fixtures."""

import json
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from app.comparison import candidate_priority, scan_comparison
from app.config import get_settings
from app.db import now
from app.discovery import registered_stores
from app.identity import match_model, match_variant, model_attributes
from app.lookup import store_listing
from app.models import DiscoveryStore, Source
from app.retail import Listing, LookupError, parse_listing, search_store, target_for


def phone():
    title = "Apple iPhone 17 (Lavender, 256 GB)"
    return Listing(
        target_for("https://www.flipkart.com/contract/p/itm123?pid=MOBTEST128"),
        title,
        "Apple",
        "mobile",
        model_attributes(title, "Apple", {"listing_title": title}),
        None,
        100000,
        None,
        "in_stock",
        "new",
        "Contract seller",
    )


def test_new_apple_comparison_searches_resellers_without_prior_product_lookup(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "discovery_retailers", [])
    product_id = store_listing(db, phone())
    calls = []

    def discover(retailer, query, expected, *, custom):
        calls.append((retailer, custom["name"]))
        return {
            "search": {"status": "no_matches", "search_url": custom["search_base"]},
            "listings": [],
            "failures": [],
        }

    monkeypatch.setattr("app.comparison.discover_store", discover)
    scan_comparison(db, product_id)
    assert {name for _, name in calls} == {"iPlanet", "Imagine"}
    assert len(calls) == 2
    assert not registered_stores(db, "Samsung")
    assert not registered_stores(db)
    monkeypatch.setattr(get_settings(), "discovery_extra_store_limit", 1)
    assert len(registered_stores(db, "Apple")) == 1
    monkeypatch.setattr(get_settings(), "discovery_extra_store_limit", 0)
    assert not registered_stores(db, "Apple")


@pytest.mark.parametrize("restriction", ["paused", "display", "history", "expired", "kind"])
def test_bootstrap_never_overrides_existing_source_or_discovery_restrictions(db, restriction):
    target = target_for("https://iplanet.one/search?")
    source = Source(
        id="public-page:" + target.retailer,
        name="Contract source",
        kind="public_page",
        display_allowed=True,
        history_allowed=True,
    )
    db.add(source)
    if restriction == "paused":
        db.add(
            DiscoveryStore(
                retailer_id=target.retailer,
                source_id=source.id,
                host="iplanet.one",
                brands=["Apple"],
                enabled=False,
                learned_at=now(),
            )
        )
    elif restriction == "expired":
        source.expires_at = now() - timedelta(seconds=1)
    elif restriction == "kind":
        source.kind = "merchant"
    else:
        setattr(source, restriction + "_allowed", False)
    db.commit()
    assert target.retailer not in registered_stores(db, "Apple")


@pytest.mark.parametrize("model,excludes_pro", [("iPhone 17", True), ("iPhone 17 Pro", False)])
def test_shopify_search_uses_vendor_and_preserves_requested_model_qualifiers(
    monkeypatch, model, excludes_pro
):
    calls = []
    metadata = {
        "products": [
            {
                "vendor": "Apple",
                "handle": "contract",
                "variants": [{"id": 123, "name": model + " 256GB Lavender", "price": 1}],
            }
        ]
    }
    monkeypatch.setattr(
        "app.retail.fetch_page",
        lambda url, retailer: (
            calls.append(url) or "<script>var meta = " + json.dumps(metadata) + ";</script>",
            url,
        ),
    )
    target = target_for("https://iplanet.one/search?")
    result = search_store(
        target.retailer,
        "Apple " + model,
        custom={"name": "iPlanet", "search_base": "https://iplanet.one/search?"},
    )
    query = parse_qs(urlsplit(calls[0]).query)["q"][0]
    assert query.startswith('"' + model + '"')
    assert ("-pro" in query.split()) == excludes_pro
    assert len(result["items"]) == 1
    assert "item_paise" not in result["items"][0]


def test_plain_phone_rejects_protection_bundle_even_with_shared_barcode():
    anchor = replace(phone(), attributes={**phone().attributes, "gtin12": "195950644043"})
    bundle = replace(anchor, title=anchor.title + " with Protect+ with AppleCare Services")
    assert match_variant(anchor, bundle)["status"] == "different_variant"
    assert match_model(anchor, bundle)["status"] == "different_variant"
    assert (
        match_model(anchor, replace(anchor, title=anchor.title + " with A19 chip"))["status"]
        == "verified"
    )
    expected = {**anchor.attributes, "_brand": "Apple"}
    assert candidate_priority({"title": anchor.title}, expected) < candidate_priority(
        {"title": bundle.title}, expected
    )
    assert candidate_priority({"title": anchor.title}, expected) < candidate_priority(
        {"title": "Apple iPhone 17 Pro 256GB Lavender"}, expected
    )


@pytest.mark.parametrize("color", ["Lavender", "Sage", "Mist Blue"])
def test_phone_colour_is_preserved_outside_parentheses(color):
    attributes = model_attributes("iPhone 17 " + color + " 256GB", "Apple", {})
    assert attributes["model"] == "iPhone 17"
    assert attributes["color"] == color.lower()


def single_offer_page(*, offer_variant=123, metadata_price=100000, host="shop.example.org"):
    item = {
        "@type": "Product",
        "name": "iPhone 17 256GB Lavender",
        "brand": "Apple",
        "sku": "",
        "offers": [
            {
                "price": "1000",
                "priceCurrency": "INR",
                "url": f"https://{host}/products/contract?variant={offer_variant}",
                "availability": "https://schema.org/OutOfStock",
            }
        ],
    }
    meta = {
        "product": {
            "handle": "contract",
            "variants": [{"id": 123, "sku": "CONTRACT-SKU", "price": metadata_price}],
        }
    }
    return (
        '<script type="application/ld+json">'
        + json.dumps(item)
        + "</script><script>var meta = "
        + json.dumps(meta)
        + ";</script>"
    )


def test_single_offer_recovers_only_corroborated_selected_sku_and_retains_stock_state():
    target = target_for("https://shop.example.org/products/contract")
    value = parse_listing(single_offer_page(), target)
    assert value.attributes["sku"] == "CONTRACT-SKU"
    assert value.target.url.endswith("?variant=123")
    assert value.discovery_kind == "shopify_html"
    assert value.availability == "out_of_stock"
    with pytest.raises(LookupError):
        parse_listing(single_offer_page(metadata_price=1), target)


@pytest.mark.parametrize("changes", [{"offer_variant": 456}, {"host": "other.example.org"}])
def test_single_offer_cannot_substitute_another_requested_variant_or_host(changes):
    with pytest.raises(LookupError):
        parse_listing(
            single_offer_page(**changes),
            target_for("https://shop.example.org/products/contract?variant=123"),
        )


def test_empty_previous_sku_can_be_enriched_but_identified_sku_cannot_change(db):
    anchor = replace(phone(), attributes={**phone().attributes, "sku": ""})
    product_id = store_listing(db, anchor)
    enriched = replace(anchor, attributes={**anchor.attributes, "sku": "CONTRACT-SKU"})
    assert store_listing(db, enriched) == product_id
    with pytest.raises(LookupError):
        store_listing(db, replace(enriched, attributes={**enriched.attributes, "sku": "OTHER-SKU"}))
