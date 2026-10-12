"""Synthetic theme contracts are isolated to the test database."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from app.config import get_settings
from app.db import now
from app.discovery import registered_stores
from app.identity import match_model, match_variant, model_attributes
from app.lookup import store_listing
from app.models import DiscoveryStore, Source
from app.retail import LookupError, parse_listing, search_store, target_for

BASE = "https://www.fireboltt.com/products/contract-orbit-black"


def evidence():
    variant = {
        "id": 101,
        "sku": "CONTRACT-101",
        "price": 120000,
        "available": False,
        "option1": "Silicone",
        "option2": "Black",
        "featured_image": {"src": "//www.fireboltt.com/cdn/shop/contract.png"},
    }
    native = {
        "id": 100,
        "handle": "contract-orbit-black",
        "vendor": "Boltt Games",
        "title": "Contract Orbit - Black",
        "type": "Smart Watch",
        "options": ["Strap Style", "Color"],
        "variants": [variant],
    }
    meta = deepcopy(native)
    offer = {
        "sku": "CONTRACT-101",
        "price": 1200,
        "priceCurrency": "INR",
        "availability": "https://schema.org/OutOfStock",
        "url": BASE + "?variant=101",
    }
    product = {
        "@type": "Product",
        "name": native["title"],
        "url": BASE,
        "brand": {"name": "Boltt Games"},
        "offers": [offer],
        "mpn": "101",
        "aggregateRating": {"ratingValue": 4.2, "ratingCount": 17},
    }
    return native, meta, product


def page(native=None, meta=None, product=None, *, tail="known", heading=None, canonical=BASE):
    n, m, p = evidence()
    native, meta, product = native or n, meta or m, product or p
    schema = json.dumps(product)
    if tail == "known":
        schema += ',"itemReviewed":' + json.dumps({"@type": "Product", "name": "Review object"})
    elif tail == "arbitrary":
        schema += ',"other":123'
    organization = {
        "@type": "Organization",
        "name": "Fire-Boltt",
        "url": "https://www.fireboltt.com/",
    }
    return (
        f'<link rel="canonical" href="{canonical}"><h1>{heading or native["title"]}</h1>'
        '<script type="application/ld+json">' + json.dumps(organization) + "</script>"
        '<script type="application/ld+json">' + schema + "</script>"
        "<script>var meta = " + json.dumps({"product": meta}) + ";</script>"
        "<script>\nvar snapProdDetail = " + json.dumps(native) + ";</script>"
    )


@pytest.mark.parametrize("tail", ["known", "valid"])
def test_price_is_correlated_and_sold_out_is_never_available(tail):
    listing = parse_listing(page(tail=tail), target_for(BASE))
    assert listing.brand == "Fire-Boltt" and listing.item_paise == 120000
    assert listing.availability == "out_of_stock" and listing.condition == "unknown"
    assert listing.attributes == {
        "sku": "CONTRACT-101",
        "listing_title": "Contract Orbit - Black",
        "model": "Contract Orbit",
        "color": "Black",
        "strap_style": "Silicone",
    }
    assert listing.target.url == BASE + "?variant=101"
    assert listing.image_url == "https://www.fireboltt.com/cdn/shop/contract.png"
    assert listing.shipping_paise is None and listing.rating["count"] == 17
    assert listing.discovery_kind == "shopify_html"


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("native", "id", 999),
        ("native", "handle", "other"),
        ("native", "vendor", "Other"),
        ("native", "title", "Other"),
        ("meta", "id", 999),
        ("meta", "vendor", "Other"),
        ("product", "url", "https://fireboltt.shop/products/contract-orbit-black"),
        ("product", "name", "Other"),
        ("product", "brand", {"name": "Other"}),
        ("variant", "price", 100),
        ("variant", "price", True),
        ("variant", "price", -1),
        ("variant", "sku", "OTHER"),
        ("variant", "available", "false"),
        ("variant", "available", True),
        ("offer", "price", 100),
        ("offer", "priceCurrency", "USD"),
        ("offer", "sku", "OTHER"),
        ("offer", "availability", "Unknown"),
        ("offer", "url", BASE + "?variant=102"),
    ],
)
def test_disagreement_fails_closed(section, key, value):
    native, meta, product = evidence()
    sections = {
        "native": native,
        "meta": meta,
        "product": product,
        "variant": native["variants"][0],
        "offer": product["offers"][0],
    }
    sections[section][key] = value
    with pytest.raises(LookupError):
        parse_listing(page(native, meta, product), target_for(BASE))


@pytest.mark.parametrize(
    "change", ["tail", "heading", "canonical", "comment", "duplicate", "captcha"]
)
def test_untrusted_or_ambiguous_page_evidence_is_rejected(change):
    html = page(
        **{"tail": "arbitrary"}
        if change == "tail"
        else {"heading": "Another product"}
        if change == "heading"
        else {"canonical": BASE + "-other"}
        if change == "canonical"
        else {}
    )
    if change == "comment":
        html = html.replace("var snapProdDetail", "// var snapProdDetail")
    if change == "duplicate":
        html += "<script>\nvar snapProdDetail = {};</script>"
    if change == "captcha":
        html += '<input id="captchacharacters">'
    with pytest.raises(LookupError):
        parse_listing(html, target_for(BASE))


@pytest.mark.parametrize("query", ["?variant=102", "?variant=101&variant=102", "?variant=oops"])
def test_requested_variant_must_be_unambiguous_and_present(query):
    with pytest.raises(LookupError) as failure:
        parse_listing(page(), target_for(BASE + query))
    assert failure.value.code == "missing_variant"


def test_explicit_variant_uses_its_own_price_color_image_and_stock():
    native, meta, product = evidence()
    variant = {
        **native["variants"][0],
        "id": 102,
        "sku": "CONTRACT-102",
        "option2": "Blue",
        "price": 130000,
        "available": True,
        "featured_image": {"src": "//cdn.shopify.com/blue.png"},
    }
    native["variants"].append(variant)
    meta["variants"].append(deepcopy(variant))
    product["offers"].append(
        {
            **product["offers"][0],
            "url": BASE + "?variant=102",
            "sku": "CONTRACT-102",
            "price": 1300,
            "availability": "https://schema.org/InStock",
        }
    )
    html = page(native, meta, product)
    with pytest.raises(LookupError):
        parse_listing(html, target_for(BASE))
    listing = parse_listing(html, target_for(BASE + "?variant=102"))
    assert listing.item_paise == 130000 and listing.availability == "in_stock"
    assert listing.attributes["color"] == "Blue" and listing.image_url.endswith("/blue.png")


def test_store_listing_keeps_sold_out_evidence_and_unknown_delivery(client, db):
    product_id = store_listing(db, parse_listing(page(), target_for(BASE)))
    db.commit()
    detail = client.get(f"/api/v1/products/{product_id}").json()
    offer = detail["offers"][0]
    assert offer["retailer_name"] == "Fire-Boltt Official Store"
    assert offer["item_paise"] == 120000 and offer["availability"] == "out_of_stock"
    assert offer["total_paise"] is None and detail["best_item_price_paise"] is None


@pytest.mark.parametrize("brand", ["Fire-Boltt", "Fireboltt", "Fire Boltt"])
def test_brand_spelling_bootstraps_official_store_only(brand, db):
    target = target_for(BASE)
    assert set(registered_stores(db, brand)) == {target.retailer}
    assert target.retailer not in registered_stores(db, "Other")


@pytest.mark.parametrize("restriction", ["paused", "display", "history", "expired", "kind"])
def test_bootstrap_never_overrides_source_controls(db, restriction):
    target = target_for(BASE)
    source = Source(
        id="public-page:" + target.retailer,
        name="Fire-Boltt",
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
                host="www.fireboltt.com",
                brands=["Fire-Boltt"],
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
    assert target.retailer not in registered_stores(db, "Fireboltt")


@pytest.mark.parametrize("brand", ["Fire-Boltt", "Fireboltt", "Fire Boltt"])
def test_fresh_name_lookup_includes_source(client, monkeypatch, brand):
    monkeypatch.setattr(get_settings(), "discovery_retailers", [])
    calls = []

    def search(retailer, query, *, custom):
        calls.append(custom["name"])
        return {
            "retailer": retailer,
            "status": "no_matches",
            "items": [],
            "search_url": custom["search_base"],
        }

    monkeypatch.setattr("app.lookup.search_store", search)
    response = client.post("/api/v1/lookup", json={"query": brand + " Contract Orbit"})
    assert response.status_code == 200 and calls == ["Fire-Boltt Official Store"]


def test_search_uses_model_query_and_ranks_base_before_pro_without_trusting_prices(monkeypatch):
    calls = []
    products = [
        {
            "vendor": "Boltt Games",
            "handle": f"contract-{i}",
            "variants": [{"id": i, "name": f"Contract Orbit{suffix} - Black", "price": 1}],
        }
        for i, suffix in enumerate([" Pro", " Plus", " Max", " Elite", " Neo", "", " SE"], 1)
    ]

    def fetch(url, retailer):
        calls.append(url)
        return "<script>var meta = " + json.dumps({"products": products}) + ";</script>", url

    monkeypatch.setattr("app.retail.fetch_page", fetch)
    result = search_store(
        target_for(BASE).retailer,
        "Fire-Boltt Contract Orbit",
        custom={
            "name": "Fire-Boltt Official Store",
            "search_base": "https://www.fireboltt.com/search?",
        },
    )
    assert parse_qs(urlsplit(calls[0]).query)["q"] == ["Contract Orbit"]
    assert result["items"][0]["url"].endswith("variant=6")
    assert all("price" not in item and "item_paise" not in item for item in result["items"])


@pytest.mark.parametrize("suffix", ["Pro", "Plus", "Elite", "Neo", "SE", "Max"])
def test_word_models_preserve_qualifiers_and_do_not_share_history(suffix):
    anchor = parse_listing(page(), target_for(BASE))
    other = replace(
        anchor,
        brand="Fireboltt",
        title=f"Fireboltt Contract Orbit {suffix} Smartwatch",
        attributes={"color": "Black"},
    )
    assert (
        model_attributes(other.title, other.brand, other.attributes)["model"]
        == "Contract Orbit " + suffix
    )
    assert match_model(anchor, other)["status"] == "different_variant"
    stale = replace(other, attributes={"model": "Contract Orbit"})
    assert match_model(anchor, stale)["status"] == "different_variant"
    same = replace(other, title='Fireboltt Contract Orbit 1.83" Display Smartwatch')
    assert match_model(anchor, same)["status"] == "model_match"
    assert match_variant(anchor, same)["status"] != "verified"


@pytest.mark.parametrize(
    "title",
    [
        "Fire\u2011Boltt Contract Orbit Pro Smart Watch 2.02\u2033 Display",
        "Fire Boltt Contract Orbit Pro 51.3mm AMOLED Smartwatch",
        "Fire-Boltt Contract Orbit Pro 1.83'' HD Display Smartwatch",
    ],
)
def test_marketplace_punctuation_keeps_named_model(title):
    assert model_attributes(title, "Fireboltt", {})["model"] == "Contract Orbit Pro"


def test_tracking_queries_and_host_aliases_resolve_to_canonical_variant():
    first = parse_listing(page(), target_for(BASE + "?ref=contract"))
    alias = parse_listing(page(), target_for(BASE.replace("www.", "") + "?variant=101&ref=other"))
    assert first.target == alias.target


@pytest.mark.parametrize("wrapper", ["comment", "template"])
def test_native_assignment_in_a_block_comment_or_template_is_not_evidence(wrapper):
    html = page()
    text = "var snapProdDetail = " + json.dumps(evidence()[0]) + ";"
    wrapped = "/*\n" + text + "\n*/" if wrapper == "comment" else "var text = `\n" + text + "\n`;"
    with pytest.raises(LookupError):
        parse_listing(html.replace(text, wrapped), target_for(BASE))


@pytest.mark.parametrize("image", [123, [], {"other": "unknown"}])
def test_invalid_image_metadata_cannot_abort_valid_price(image):
    native, meta, product = evidence()
    native["variants"][0]["featured_image"] = {"src": image}
    listing = parse_listing(page(native, meta, product), target_for(BASE))
    assert listing.item_paise == 120000 and listing.image_url is None


def test_cross_store_model_comparison_retains_independent_histories(client, db, monkeypatch):
    from app.comparison import scan_comparison

    monkeypatch.setattr(get_settings(), "discovery_retailers", ["amazon"])
    anchor = parse_listing(page(), target_for(BASE))
    anchor_id = store_listing(db, anchor)
    matching = replace(
        anchor,
        target=target_for("https://www.amazon.in/dp/B0TEST0021"),
        title="Fire-Boltt Contract Orbit Smartwatch",
        attributes={"model": "Contract Orbit", "color": "Blue"},
        item_paise=110000,
        availability="in_stock",
        discovery_kind=None,
    )
    qualified = replace(
        matching,
        target=target_for("https://www.amazon.in/dp/B0TEST0022"),
        title="Fire-Boltt Contract Orbit Pro Smartwatch",
        attributes={"model": "Contract Orbit Pro"},
        item_paise=100,
    )
    monkeypatch.setattr(
        "app.comparison.discover_store",
        lambda *args, **kwargs: {
            "search": {"status": "ok", "search_url": "https://www.amazon.in/s?"},
            "listings": [matching, qualified],
            "failures": [],
        },
    )
    scan = scan_comparison(db, anchor_id)
    assert [offer["item_paise"] for offer in scan["model_offers"]] == [110000]
    assert scan["model_offers"][0]["product_id"] != anchor_id
    exact = client.get(f"/api/v1/products/{anchor_id}").json()
    assert len(exact["offers"]) == 1 and exact["best_item_price_paise"] is None
    assert exact["offers"][0]["item_paise"] == 120000
