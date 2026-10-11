"""Coverage regressions using isolated stores; never fetch or seed live prices."""

from dataclasses import replace

from sqlalchemy import select

from app.comparison import link_catalog_matches, model_offers, scan_comparison
from app.config import Settings, get_settings
from app.db import now
from app.discovery import registered_stores
from app.lookup import store_listing
from app.models import Observation, ProductMatch, Source
from app.retail import Listing, target_for


def listing(host="www.amazon.in", *, barcode=False):
    target = target_for(
        "https://www.amazon.in/dp/B012345678"
        if host == "www.amazon.in"
        else f"https://{host}/products/contract?variant=123"
    )
    attributes = {"model": "WH-520", "color": "Blue", "sku": "TEST520"}
    if barcode:
        attributes["gtin13"] = "4006381333931"
    return Listing(
        target,
        "Test WH-520 Blue Headphones",
        "Test",
        "electronics",
        attributes,
        None,
        100000,
        0,
        "in_stock",
        "new",
        "Contract seller",
    )


def test_all_registered_stores_are_checked_by_default_and_permissions_still_apply(db, monkeypatch):
    assert Settings(_env_file=None).discovery_extra_store_limit == -1
    monkeypatch.setattr(get_settings(), "discovery_extra_store_limit", -1)
    monkeypatch.setattr(get_settings(), "discovery_retailers", [])
    anchor = listing()
    anchor_id = store_listing(db, anchor)
    for index in range(12):
        store_listing(
            db, replace(listing(f"store-{index}.example.org"), discovery_kind="shopify_html")
        )
    stores = registered_stores(db, "Test")
    assert len(stores) == 12
    calls = []

    def discover(retailer, query, expected, *, custom):
        calls.append(retailer)
        return {
            "search": {"status": "no_matches", "search_url": custom["search_base"]},
            "listings": [],
            "failures": [],
        }

    monkeypatch.setattr("app.comparison.discover_store", discover)
    result = scan_comparison(db, anchor_id)
    assert set(calls) == set(stores)
    assert len(result["stores"]) == 12
    assert scan_comparison(db, anchor_id)["cached"]
    revoked = next(iter(stores))
    db.get(Source, "public-page:" + revoked).display_allowed = False
    db.commit()
    assert len(registered_stores(db, "Test")) == 11
    calls.clear()
    result = scan_comparison(db, anchor_id)
    assert not result["cached"]
    assert len(calls) == 11 and revoked not in calls


def test_all_matching_catalog_offers_survive_multiple_database_batches(db):
    anchor = listing()
    anchor_id = store_listing(db, anchor)
    ids = set()
    for index in range(111):
        ids.add(store_listing(db, listing(f"store-{index}.example.org")))
    wrong_model = replace(listing("other-model.example.org"), attributes={"model": "WH-999"})
    store_listing(db, wrong_model)
    offers = model_offers(db, anchor_id, anchor)
    assert {offer["product_id"] for offer in offers} == ids
    assert len(offers) == 111
    assert not list(db.scalars(select(ProductMatch)))


def test_exact_matches_are_not_cut_off_after_one_hundred_catalog_candidates(db):
    anchor = listing(barcode=True)
    anchor_id = store_listing(db, anchor)
    ids = {
        store_listing(db, listing(f"store-{index}.example.org", barcode=True))
        for index in range(111)
    }
    link_catalog_matches(db, anchor_id, anchor)
    pairs = list(db.scalars(select(ProductMatch)))
    assert len(pairs) == 111
    assert {pair.right_id if pair.left_id == anchor_id else pair.left_id for pair in pairs} == ids


def test_distinct_checks_with_same_clock_tick_do_not_reuse_observation_event_ids(db, monkeypatch):
    at = now()
    monkeypatch.setattr("app.lookup.now", lambda: at)
    anchor = listing()
    product_id = store_listing(db, anchor)
    store_listing(db, replace(anchor, item_paise=99000))
    rows = list(
        db.scalars(select(Observation).where(Observation.offer_id == product_id + ":public-page"))
    )
    assert len(rows) == 2
    assert len({row.external_event_id for row in rows}) == 2
    assert {row.item_paise for row in rows} == {100000, 99000}
