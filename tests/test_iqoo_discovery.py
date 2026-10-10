import json
from dataclasses import replace

import pytest
from bs4 import BeautifulSoup

from app.comparison import candidate_priority, discover_store, discovery_query
from app.identity import match_variant, mobile_attributes
from app.iqoo import catalog_rows, state, variant_rows
from app.retail import LookupError, amazon_condition, discovery_retailers, parse_listing, target_for

TITLE = (
    "iQOO Z11 Lite 44W 5G (Solar Flame, 4GB RAM, 128GB Storage) | 6500mAh Battery | Dimensity 6300"
)


def test_mobile_title_without_phone_word_retains_multiword_colour_and_clean_model():
    attributes = mobile_attributes(TITLE, "iQOO", {})
    assert attributes == {
        "ram_gb": 4,
        "storage_gb": 128,
        "color": "Solar Flame",
        "model": "Z11 Lite 44W 5G",
        "identity_category": "mobile",
    }


def test_storage_before_explicit_ram_does_not_swap_the_two_capacities():
    value = mobile_attributes("Test Phone Blue 128 GB 6 GB RAM", "Test", {})
    assert value["ram_gb"] == 6 and value["storage_gb"] == 128


def test_missing_charging_label_requires_review_without_asserting_a_different_variant():
    from app.retail import Listing

    first = Listing(
        target_for("https://www.amazon.in/dp/B0H1WY1D37"),
        TITLE,
        "iQOO",
        "mobile",
        mobile_attributes(TITLE, "iQOO", {}),
        None,
        1900000,
        None,
        "in_stock",
        "new",
        "Test seller",
    )
    second = replace(first, attributes={**first.attributes, "model": "Z11 Lite 5G"})
    assert match_variant(first, second)["status"] == "review"
    assert (
        match_variant(first, replace(second, attributes={**second.attributes, "ram_gb": 6}))[
            "status"
        ]
        == "different_variant"
    )


@pytest.mark.parametrize(
    "title",
    [
        "Samsung Galaxy Book 16GB RAM 512GB Storage",
        "iQOO phone case 4GB RAM 128GB Storage",
        "Apple iPad tablet 8GB RAM 128GB Storage",
    ],
)
def test_non_phone_memory_titles_are_not_classified_as_phones(title):
    assert "identity_category" not in mobile_attributes(title, title.split()[0], {})


def test_requested_memory_and_colour_rank_before_cheaper_different_variants(monkeypatch):
    expected = mobile_attributes(TITLE, "iQOO", {})
    items = [
        {
            "title": "IQOO Z11 lite 5G (Solar Flame, 128 GB) 6 GB RAM",
            "url": "https://www.flipkart.com/phone/p/itm1?pid=MOBWRONG6",
        },
        {
            "title": "IQOO Z11 lite 5G (Midnight Blue, 128 GB) 6 GB RAM",
            "url": "https://www.flipkart.com/phone/p/itm1?pid=MOBWRONGB",
        },
        {
            "title": "IQOO Z11 lite 5G (Solar Flame, 128 GB) 4 GB RAM",
            "url": "https://www.flipkart.com/phone/p/itm1?pid=MOBEXACT4",
        },
    ]
    assert candidate_priority(items[2], expected) < candidate_priority(items[0], expected)
    calls = []
    monkeypatch.setattr("app.comparison.search_store", lambda *args: {"items": items})

    def load(target):
        calls.append(target.external_id)
        raise LookupError("price_unavailable", "Controlled unreadable page")

    monkeypatch.setattr("app.comparison.load_listing", load)
    discover_store("flipkart", "iQOO Z11 Lite", expected)
    assert calls[0] == "MOBEXACT4" and len(calls) == 2


def test_candidate_ranking_uses_explicit_card_specs_when_title_omits_ram():
    expected = mobile_attributes(TITLE, "iQOO", {})
    title = "IQOO Z11 lite 5G (Solar Flame, 128 GB)"
    four = {
        "title": title,
        "variant_attributes": {"ram_gb": 4, "storage_gb": 128, "color": "Solar Flame"},
    }
    six = {
        "title": title,
        "variant_attributes": {"ram_gb": 6, "storage_gb": 128, "color": "Solar Flame"},
    }
    assert candidate_priority(four, expected) < candidate_priority(six, expected)


@pytest.mark.parametrize(
    "asin,types,expected",
    [
        ("B0H1WY1D37", ["NEW"], "new"),
        ("OTHERASIN1", ["NEW"], "unknown"),
        ("B0H1WY1D37", ["NEW", "USED"], "unknown"),
    ],
)
def test_amazon_condition_requires_explicit_single_option_for_selected_asin(asin, types, expected):
    data = json.dumps({"productAsin": asin, "buyingOptionTypes": types})
    html = '<script type="a-state" data-a-state=\'{"key":"vas-base-vm"}\'>' + data + "</script>"
    assert amazon_condition(BeautifulSoup(html, "html.parser"), "B0H1WY1D37") == expected


def test_official_sku_url_is_canonical_and_does_not_lose_selected_variant():
    target = target_for("https://shop.iqoo.com/in/product/2077?skuId=8478&tracking=1")
    assert target.retailer == "iqoo" and target.external_id == "2077-8478"
    assert target.url == "https://shop.iqoo.com/in/product/2077?skuId=8478"
    assert "iqoo" in discovery_retailers("mobile", "iQOO")
    assert "iqoo" not in discovery_retailers("mobile", "Nothing")


def nuxt(body, names="a,b", values='"Z11 Lite 44W 5G",8478'):
    return (
        "<script>window.__NUXT__=(function(" + names + "){" + body + "}(" + values + "));</script>"
    )


def test_public_nuxt_metadata_is_read_as_literals_without_executing_code():
    html = nuxt(
        'throw new Error("must never execute");return {productList:[{shortName:a,skuName:"Z11 Lite 44W 5G 4GB+128GB Midnight Blue",skuId:8477,spuId:2077,energyInfo:{}}]};'
    )
    assert catalog_rows(html)[0]["spuId"] == 2077
    html = nuxt(
        'rows[0]={skuId:b,shortName:a,skuName:"Z11 Lite 44W 5G 4GB+128GB Solar Flame"};return {};'
    )
    assert variant_rows(html) == [{"skuId": 8478, "title": "Z11 Lite 44W 5G 4GB+128GB Solar Flame"}]
    assert state('<script>window.__NUXT__=fetch("https://example.org")</script>') == ("", {})


def test_official_listing_checks_selected_sku_and_uses_selected_colour():
    product = {
        "@type": "Product",
        "name": "Z11 Lite 44W 5G",
        "sku": "8478",
        "brand": {"name": "iQOO"},
        "offers": {
            "price": "19999",
            "priceCurrency": "INR",
            "availability": "https://schema.org/InStock",
            "itemCondition": "https://schema.org/NewCondition",
        },
    }
    html = (
        '<h1 class="title">Z11 Lite 44W 5G 4GB+128GB Solar Flame</h1><div role="radio" aria-checked="true"><div class="color-name">Solar Flame</div></div><script type="application/ld+json">'
        + json.dumps(product)
        + "</script>"
    )
    listing = parse_listing(html, target_for("https://shop.iqoo.com/in/product/2077?skuId=8478"))
    assert listing.attributes["color"] == "Solar Flame" and listing.attributes["ram_gb"] == 4
    assert listing.title.endswith("Solar Flame") and listing.condition == "new"
    assert discovery_query(listing) == "iQOO Z11 Lite"
    with pytest.raises(LookupError, match="requested product SKU"):
        parse_listing(html, target_for("https://shop.iqoo.com/in/product/2077?skuId=8477"))


def test_official_search_reads_one_catalog_and_one_model_then_returns_real_sku_urls(monkeypatch):
    from app.retail import search_store

    catalog = nuxt(
        'return {productList:[{shortName:a,skuName:"Z11 Lite 44W 5G 4GB+128GB Midnight Blue",skuId:8477,spuId:2077,energyInfo:{}}]};'
    )
    product = nuxt(
        'rows[0]={skuId:b,shortName:a,skuName:"Z11 Lite 44W 5G 4GB+128GB Solar Flame"};return {};'
    )
    calls = []

    def fetch(url, retailer):
        calls.append(url)
        return (catalog if "/products/" in url else product), url

    monkeypatch.setattr("app.retail.fetch_page", fetch)
    result = search_store("iqoo", "iQOO Z11 Lite")
    assert len(calls) == 2 and len(result["items"]) == 2
    assert result["items"][0]["url"].endswith("?skuId=8478")
