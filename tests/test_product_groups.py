import json

import pytest

from app.retail import LookupError, parse_listing, target_for


def page(*, price=899, metadata_price=89900, sku="SKU60", child_url=None):
    url = "https://shop.example.org/products/serum?variant=60"
    child = {
        "@type": "Product",
        "name": "Test Serum 60ml",
        "sku": sku,
        "offers": {
            "price": price,
            "priceCurrency": "INR",
            "availability": "https://schema.org/InStock",
            "url": child_url or url,
        },
    }
    group = {
        "@type": "ProductGroup",
        "url": url.split("?")[0],
        "name": "Test Serum",
        "brand": {"name": "Test"},
        "category": "Skin Care",
        "hasVariant": [child],
    }
    # A stale parent-level minimum must never become the selected 60ml price.
    parent = {
        "@type": "Product",
        "name": "Test Serum",
        "brand": {"name": "Test"},
        "sku": "SKU60",
        "offers": {"price": 237, "priceCurrency": "INR", "url": url},
    }
    meta = {
        "product": {
            "handle": "serum",
            "variants": [{"id": 60, "sku": "SKU60", "price": metadata_price}],
        }
    }
    return (
        '<script type="application/ld+json">'
        + json.dumps([parent, group])
        + "</script><script>var meta = "
        + json.dumps(meta)
        + ';</script><form action="/cart/add"><input name="id" value="60"></form>'
    )


def test_selected_group_requires_corroborated_sku_price_and_inherits_brand():
    value = parse_listing(page(), target_for("https://shop.example.org/products/serum?variant=60"))
    assert value.item_paise == 89900
    assert value.brand == "Test"
    assert value.title == "Test Serum 60ml"
    assert value.discovery_kind == "shopify_html"
    assert value.shipping_paise is None


def test_unambiguous_form_variant_is_retained_in_canonical_listing():
    value = parse_listing(page(), target_for("https://shop.example.org/products/serum"))
    assert value.target.url.endswith("?variant=60")
    assert value.attributes["sku"] == "SKU60"


@pytest.mark.parametrize(
    "changes",
    [
        {"metadata_price": 23700},
        {"sku": "SKU30"},
        {"metadata_price": True},
        {"child_url": "https://evil.example.org/products/serum?variant=60"},
        {"child_url": "https://shop.example.org/products/other?variant=60"},
    ],
)
def test_conflicts_and_other_products_cannot_supply_selected_group_price(changes):
    with pytest.raises(LookupError):
        parse_listing(
            page(**changes), target_for("https://shop.example.org/products/serum?variant=60")
        )
