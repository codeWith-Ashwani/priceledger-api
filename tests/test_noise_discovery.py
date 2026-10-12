"""Controlled source contracts; these products are never inserted in the live catalog."""

import json
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from app.comparison import discover_store, scan_comparison
from app.config import get_settings
from app.db import now
from app.discovery import registered_stores
from app.identity import match_model, match_variant, model_attributes
from app.lookup import store_listing
from app.models import DiscoveryStore, Source
from app.retail import LookupError, parse_listing, search_store, target_for

BASE = "https://www.gonoise.com/products/contract-buds-vs102"


def group_page(*, group_id=100, vendor="Noise", price=999):
    group = {
        "@type": "ProductGroup",
        "productGroupID": group_id,
        "url": BASE,
        "name": "Noise Buds VS102 Wireless Earbuds",
        "brand": {"name": "Noise"},
        "category": "Wireless Earbuds",
        "aggregateRating": {"ratingValue": 4.4, "ratingCount": 23},
        "hasVariant": [
            {
                "@type": "Product",
                "name": f"Noise Buds VS102 Wireless Earbuds {color}",
                "sku": f"CONTRACT-{variant}",
                "color": color,
                "offers": {
                    "price": price,
                    "priceCurrency": "INR",
                    "availability": "https://schema.org/InStock",
                    "url": BASE + f"?variant={variant}",
                },
            }
            for variant, color in [(1, "Black"), (2, "White")]
        ],
    }
    metadata = {
        "product": {
            "id": 100,
            "vendor": vendor,
            "handle": "contract-buds-vs102",
            "variants": [
                {"id": variant, "sku": f"CONTRACT-{variant}", "price": 99900} for variant in [1, 2]
            ],
        }
    }
    return (
        '<script type="application/ld+json">'
        + json.dumps(group)
        + "</script><script>var meta = "
        + json.dumps(metadata)
        + ";</script>"
    )


def test_unselected_group_identifies_model_without_inventing_a_variant_price():
    listing = parse_listing(group_page(), target_for(BASE))
    assert listing.title == "Noise Buds VS102 Wireless Earbuds"
    assert listing.attributes["model"] == "Buds VS102"
    assert "sku" not in listing.attributes and "color" not in listing.attributes
    assert listing.item_paise is None and listing.shipping_paise is None
    assert listing.availability == listing.condition == "unknown"
    assert listing.rating["value"] == 4.4


def test_selected_group_price_and_rating_keep_variant_identity():
    listing = parse_listing(group_page(), target_for(BASE + "?variant=2"))
    assert listing.attributes["sku"] == "CONTRACT-2"
    assert listing.attributes["color"] == "White"
    assert listing.item_paise == 99900 and listing.rating["count"] == 23
    assert listing.discovery_kind == "shopify_html"
    assert listing.condition == "unknown" and listing.shipping_paise is None


@pytest.mark.parametrize("changes", [{"group_id": 101}, {"vendor": "Other"}])
def test_other_parent_identity_cannot_be_used_as_a_model_anchor(changes):
    with pytest.raises(LookupError):
        parse_listing(group_page(**changes), target_for(BASE))


def test_unknown_variant_and_conflicting_price_cannot_fall_back_to_unpriced_parent():
    with pytest.raises(LookupError):
        parse_listing(group_page(), target_for(BASE + "?variant=3"))
    with pytest.raises(LookupError) as failure:
        parse_listing(group_page(price=299), target_for(BASE + "?variant=1"))
    assert failure.value.code == "ambiguous_price"


@pytest.mark.parametrize("suffix", ["Elite", "Neo", "SE", "Pro", "+"])
def test_qualified_models_do_not_mix_with_the_base_even_with_old_metadata(suffix):
    anchor = parse_listing(group_page(), target_for(BASE + "?variant=1"))
    other = replace(
        anchor,
        title=f"Noise Buds VS102{suffix if suffix == '+' else ' ' + suffix} Wireless Earbuds",
        attributes={"model": "Buds VS102"},
    )
    assert match_model(anchor, other)["status"] == "different_variant"


def test_plus_is_a_model_qualifier_and_equivalent_to_spelled_plus():
    anchor = parse_listing(group_page(), target_for(BASE + "?variant=1"))
    plus = replace(anchor, attributes={"model": "Buds VS102+"})
    assert match_variant(anchor, plus)["status"] == "different_variant"
    assert match_model(anchor, plus)["status"] == "different_variant"
    assert (
        model_attributes("Noise Buds VS102+ Wireless Earbuds", "Noise", {})["model"]
        == "Buds VS102 Plus"
    )
    assert (
        match_model(plus, replace(plus, attributes={"model": "Buds VS102 Plus"}))["status"]
        != "different_variant"
    )


@pytest.mark.parametrize("restriction", ["paused", "display", "history", "expired", "kind"])
def test_noise_bootstrap_is_brand_scoped_and_respects_owner_restrictions(db, restriction):
    stores = registered_stores(db, "Noise")
    target = target_for(BASE)
    assert set(stores) == {target.retailer}
    assert stores[target.retailer]["name"] == "Noise Official Store"
    assert target.retailer not in registered_stores(db, "Apple")
    source = Source(
        id="public-page:" + target.retailer,
        name="Noise",
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
                host="www.gonoise.com",
                brands=["Noise"],
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
    assert target.retailer not in registered_stores(db, "Noise")


def test_noise_search_uses_plain_query_and_ranks_base_model_after_related_generations(monkeypatch):
    calls = []

    def fetch(url, retailer):
        calls.append(url)
        products = [
            {
                "vendor": "Noise",
                "handle": f"contract-{index}",
                "variants": [{"id": index, "name": f"Noise Buds VS102 {suffix} Wireless Earbuds"}],
            }
            for index, suffix in enumerate(
                ["Pro", "Elite", "Neo", "Plus", "Pro", "Elite", "", "SE"], start=1
            )
        ]
        return "<script>var meta = " + json.dumps({"products": products}) + ";</script>", url

    monkeypatch.setattr("app.retail.fetch_page", fetch)
    custom = {"name": "Noise Official Store", "search_base": "https://www.gonoise.com/search?"}
    result = search_store(target_for(BASE).retailer, "Noise Buds VS102", custom=custom)
    query = parse_qs(urlsplit(calls[-1]).query)["q"][0]
    assert query == "Noise Buds VS102"
    assert result["items"][0]["url"].endswith("?variant=7")
    search_store(target_for(BASE).retailer, "Noise Buds VS102 Elite", custom=custom)
    assert parse_qs(urlsplit(calls[-1]).query)["q"][0] == "Noise Buds VS102 Elite"


def test_fresh_brand_model_search_includes_official_store_without_prior_registration(
    client, monkeypatch
):
    monkeypatch.setattr(get_settings(), "discovery_retailers", [])
    calls = []

    def search(retailer, query, *, custom):
        calls.append(custom["name"])
        return {
            "retailer": retailer,
            "status": "no_matches",
            "search_url": custom["search_base"],
            "items": [],
        }

    monkeypatch.setattr("app.lookup.search_store", search)
    response = client.post("/api/v1/lookup", json={"query": "Noise Buds VS102"})
    assert response.status_code == 200 and calls == ["Noise Official Store"]


def test_unpriced_parent_prefers_its_own_verified_variants_over_partner_pages(monkeypatch):
    anchor = parse_listing(group_page(), target_for(BASE))
    items = [
        {"title": "Noise Buds VS102 Wireless Earbuds Black", "url": url}
        for url in [
            BASE + "-partner?variant=8",
            BASE + "-partner?variant=9",
            BASE + "?variant=1",
            BASE + "?variant=2",
        ]
    ]
    monkeypatch.setattr("app.comparison.search_store", lambda *args, **kwargs: {"items": items})
    checked = []

    def load(target):
        checked.append(target.url)
        return parse_listing(group_page(), target)

    monkeypatch.setattr("app.comparison.load_listing", load)
    result = discover_store(
        anchor.target.retailer,
        "Noise Buds VS102",
        {**anchor.attributes, "_brand": "Noise", "_url": BASE},
        custom={},
    )
    assert checked == [BASE + "?variant=1", BASE + "?variant=2"]
    assert len(result["listings"]) == 2


def test_unpriced_parent_searches_its_own_variants_without_merging_history(client, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "discovery_retailers", [])
    anchor = parse_listing(group_page(), target_for(BASE))
    anchor_id = store_listing(db, anchor)
    calls = []

    def discover(retailer, query, expected, *, custom):
        calls.append(retailer)
        return {
            "search": {"status": "ok", "search_url": custom["search_base"]},
            "listings": [parse_listing(group_page(), target_for(BASE + "?variant=1"))],
            "failures": [],
        }

    monkeypatch.setattr("app.comparison.discover_store", discover)
    result = scan_comparison(db, anchor_id)
    assert calls == [anchor.target.retailer]
    assert result["stores"][0]["status"] == "model_match"
    assert result["model_offers"][0]["item_paise"] == 99900
    assert result["model_offers"][0]["product_id"] != anchor_id
    detail = client.get(f"/api/v1/products/{anchor_id}").json()
    assert detail["best_item_price_paise"] is None and len(detail["offers"]) == 1
    variant_id = result["model_offers"][0]["product_id"]
    calls.clear()
    assert scan_comparison(db, variant_id)["stores"] == []
    assert calls == []
