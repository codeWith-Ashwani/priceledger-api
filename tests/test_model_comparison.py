import json
from dataclasses import replace

import pytest
from sqlalchemy import func, select

from app.comparison import discovery_query, scan_comparison
from app.config import get_settings
from app.identity import match_model, model_attributes
from app.lookup import store_listing
from app.models import Observation, Product, ProductMatch, Source
from app.retail import Listing, LookupError, parse_listing, search_store, target_for


def phone():
    return Listing(
        target_for("https://www.amazon.in/dp/B012345678"),
        "Contract Phone 4 (Blue, 6GB RAM, 128GB Storage)",
        "Contract",
        "mobile",
        {
            "model": "Phone 4",
            "color": "Blue",
            "ram_gb": 6,
            "storage_gb": 128,
            "identity_category": "mobile",
        },
        None,
        3000000,
        0,
        "in_stock",
        "new",
        "Contract seller",
    )


def test_model_alternatives_rank_separately_and_never_merge_history(client, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "discovery_retailers", [])
    anchor = phone()
    anchor_id = store_listing(db, anchor)
    candidate = replace(
        anchor,
        target=target_for("https://www.flipkart.com/contract/p/itm123?pid=MOBTEST128"),
        attributes={**anchor.attributes, "color": "Red", "ram_gb": 8},
        item_paise=2900000,
    )
    candidate_id = store_listing(db, candidate)
    scan = scan_comparison(db, anchor_id)
    assert scan["model_offers"][0]["product_id"] == candidate_id
    assert scan["model_offers"][0]["match_scope"] == "model"
    assert {diff["attribute"] for diff in scan["model_offers"][0]["differences"]} == {
        "color",
        "ram_gb",
    }
    assert db.scalar(select(func.count()).select_from(ProductMatch)) == 0
    assert client.get(f"/api/v1/products/{anchor_id}").json()["best_price_paise"] == 3000000
    assert len(client.get(f"/api/v1/products/{anchor_id}/history").json()["series"]) == 1
    assert scan_comparison(db, anchor_id)["cached"]
    db.get(Source, "public-page:flipkart").display_allowed = False
    db.commit()
    assert not scan_comparison(db, anchor_id)["model_offers"]


@pytest.mark.parametrize(
    "change",
    [
        {"brand": "Other"},
        {"attributes": {"model": "Phone 4 Pro"}},
        {"title": "Contract Phone 4 case", "attributes": {"model": "Phone 4"}},
        {"condition": "used"},
    ],
)
def test_related_models_and_accessories_cannot_enter_model_comparison(change):
    assert match_model(phone(), replace(phone(), **change))["status"] != "model_match"


def test_charging_label_is_optional_but_connectivity_and_generation_are_preserved():
    anchor = replace(phone(), attributes={"model": "Z11 Lite 44W 5G"})
    assert (
        match_model(anchor, replace(anchor, attributes={"model": "Z11 Lite 5G"}))["status"]
        == "model_match"
    )
    assert (
        match_model(anchor, replace(anchor, attributes={"model": "Z11 Lite 4G"}))["status"]
        == "different_variant"
    )


@pytest.mark.parametrize(
    "title,brand,expected",
    [
        ("Sony WH-CH520, Wireless On-Ear Bluetooth Headphones with Mic", "Sony", "WH-CH520"),
        ("Logitech M185 Wireless Mouse", "Logitech", "M185"),
        ("boAt Airdopes 141 | Wireless Earbuds", "boAt", "Airdopes 141"),
        ("boAt Airdopes 141 Pro | Wireless Earbuds", "boAt", "Airdopes 141 Pro"),
        ("boAt Airdopes 141 Gen 2, 4 Mics", "boAt", "Airdopes 141 Gen 2"),
        ("Nike Downshifter 13 Men's Running Shoes", "Nike", "Downshifter 13"),
    ],
)
def test_retailer_model_extraction_ignores_marketing(title, brand, expected):
    attributes = model_attributes(title, brand, {})
    assert attributes["model"] == expected
    assert attributes.get("identity_category") != "mobile"


def test_generic_size_is_not_a_model_and_legacy_headphone_identity_is_repaired_in_memory():
    assert "model" not in model_attributes("Football Ball Size 5 First Kick", "Kipsta", {})
    listing = replace(phone(), title="Sony WH-CH520, Wireless Bluetooth Headphones", brand="Sony")
    assert discovery_query(listing) == "Sony WH-CH520"


def test_generation_two_cannot_be_grouped_with_original_model():
    anchor = replace(phone(), title="boAt Airdopes 141 | Earbuds", brand="boAt", attributes={})
    other = replace(anchor, title="boAt Airdopes 141 Gen 2, 4 Mics")
    assert match_model(anchor, other)["status"] == "different_variant"
    legacy = replace(other, attributes={"model": "Airdopes 141"})
    assert match_model(anchor, legacy)["status"] == "different_variant"


def test_legacy_headphone_refresh_repairs_classification_without_losing_history(db):
    title = "Sony WH-CH520, Wireless Bluetooth Headphones"
    legacy = replace(
        phone(),
        title=title,
        brand="Sony",
        attributes={
            "model": "WH-CH520, Wireless Bluetooth Headphones",
            "identity_category": "mobile",
        },
    )
    product_id = store_listing(db, legacy)
    fresh = replace(legacy, attributes=model_attributes(title, "Sony", {}))
    assert store_listing(db, fresh) == product_id
    assert db.get(Product, product_id).attributes["model"] == "WH-CH520"
    assert db.scalar(select(func.count()).select_from(Observation)) == 2
    with pytest.raises(LookupError, match="variant metadata has changed"):
        store_listing(db, replace(fresh, attributes={"model": "WH-CH720"}))


def test_duplicate_search_aliases_record_one_observation(db, monkeypatch):
    anchor = phone()
    product_id = store_listing(db, anchor)
    candidate = replace(
        anchor, target=target_for("https://www.flipkart.com/contract/p/itm123?pid=MOBTEST128")
    )
    monkeypatch.setattr(get_settings(), "discovery_retailers", ["flipkart"])
    monkeypatch.setattr(
        "app.comparison.discover_store",
        lambda *args: {
            "search": {"status": "ok", "search_url": "https://www.flipkart.com/search?q=contract"},
            "listings": [candidate, candidate],
            "failures": [],
        },
    )
    result = scan_comparison(db, product_id)
    assert len(result["stores"][0]["items"]) == 1
    assert db.scalar(select(func.count()).select_from(Observation)) == 2


def schema_html(products, extra=""):
    return '<script type="application/ld+json">' + json.dumps(products) + "</script>" + extra


def product():
    return {
        "@type": "Product",
        "name": "Contract M185",
        "sku": "ONE",
        "brand": {"name": "Contract"},
        "offers": {
            "@type": "Offer",
            "price": 399,
            "priceCurrency": "INR",
            "availability": "InStock",
        },
    }


def test_complementary_duplicate_schemas_are_combined_but_conflicting_prices_are_rejected():
    target = target_for("https://public-retailer.org/products/contract")
    first = product()
    second = {
        **first,
        "size": ["5"],
        "offers": {**first["offers"], "itemCondition": "https://schema.org/NewCondition"},
    }
    result = parse_listing(schema_html([first, second]), target)
    assert result.item_paise == 39900 and result.attributes["size"] == "5"
    assert result.condition == "new"
    second["offers"]["price"] = 299
    with pytest.raises(LookupError):
        parse_listing(schema_html([first, second]), target)


def test_multi_offer_selection_uses_requested_variant_and_selected_stock_not_cheapest():
    target = target_for("https://shopatsc.com/products/contract?variant=22")
    item = product()
    item["offers"] = [
        {
            **item["offers"],
            "price": 199,
            "sku": "BLACK",
            "url": "https://shopatsc.com/products/contract?variant=11",
        },
        {
            **item["offers"],
            "price": 399,
            "sku": "PINK",
            "url": "https://shopatsc.com/products/contract?variant=22",
        },
    ]
    form = '<form action="/cart/add"><select name="id"><option selected value="22">Pink - Sold out</option></select></form>'
    result = parse_listing(schema_html(item, form), target)
    assert result.item_paise == 39900
    assert result.attributes["sku"] == "PINK" and result.attributes["color"] == "Pink"
    assert result.availability == "out_of_stock"
    payload = {
        "handle": "contract",
        "variants": [
            {"id": 22, "sku": "PINK", "featured_image": {"src": "//shopatsc.com/pink.jpg"}}
        ],
    }
    extra = (
        '<script type="application/json" id="ProductJson-template">'
        + json.dumps(payload)
        + "</script>"
    )
    assert (
        parse_listing(schema_html(item, form + extra), target).image_url
        == "https://shopatsc.com/pink.jpg"
    )
    payload["handle"] = "another-product"
    extra = (
        '<script type="application/json" id="ProductJson-template">'
        + json.dumps(payload)
        + "</script>"
    )
    assert parse_listing(schema_html(item, form + extra), target).image_url is None
    payload["handle"] = "contract"
    extra = (
        "<script>var json_product = "
        + json.dumps(payload)
        + '; throw new Error("do not execute");</script>'
    )
    assert (
        parse_listing(schema_html(item, form + extra), target).image_url
        == "https://shopatsc.com/pink.jpg"
    )
    with pytest.raises(LookupError):
        parse_listing(
            schema_html(item), target_for("https://shopatsc.com/products/contract?variant=33")
        )


def test_shopify_search_reads_json_handles_and_never_promotes_metadata_prices(monkeypatch):
    metadata = {
        "products": [
            {
                "vendor": "Sony",
                "handle": "contract",
                "variants": [{"id": 11, "name": "Sony WH-CH520 Black", "price": 1}],
            }
        ]
    }
    monkeypatch.setattr(
        "app.retail.fetch_page",
        lambda *args: (
            "<script>var meta = "
            + json.dumps(metadata)
            + '; throw new Error("never execute");</script>',
            args[0],
        ),
    )
    result = search_store("sony", "Sony WH-CH520")
    assert result["status"] == "ok"
    assert result["items"][0]["url"].endswith("/products/contract?variant=11")
    assert "price" not in result["items"][0]


def test_unpriced_product_can_start_discovery_without_zero_price_or_stock_claim(
    client, db, monkeypatch
):
    item = product()
    item.pop("offers")
    target = target_for("https://public-retailer.org/products/contract")
    listing = parse_listing(schema_html(item), target)
    assert listing.item_paise is None and listing.availability == "unknown"
    product_id = store_listing(db, listing)
    detail = client.get(f"/api/v1/products/{product_id}").json()
    assert detail["offers"][0]["item_paise"] is None
    assert detail["best_item_price_paise"] is None
    assert not detail["offers"][0]["in_stock"]
    monkeypatch.setattr(get_settings(), "discovery_retailers", [])
    assert scan_comparison(db, product_id)["product_id"] == product_id
