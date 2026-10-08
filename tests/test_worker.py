from datetime import timedelta

from app.db import now
from app.lookup import store_listing
from app.models import Offer, Source
from app.retail import Listing, LookupError, Target
from app.worker import refresh_once


def make_due_listing(db, key):
    target = Target(
        "flipkart", f"https://www.flipkart.com/test/p/itm{key}?pid=MOBTEST{key}", f"MOBTEST{key}"
    )
    listing = Listing(
        target,
        f"Collector Test {key}",
        "Test",
        "mobile",
        {"sku": target.external_id},
        None,
        10000,
        0,
        "in_stock",
        "new",
        "Test",
    )
    product_id = store_listing(db, listing)
    db.get(Offer, product_id + ":public-page").current_observed_at = now() - timedelta(hours=1)
    db.commit()
    return listing


def test_collector_only_refreshes_due_permitted_sources(db, monkeypatch):
    listing = make_due_listing(db, "123")
    calls = []
    monkeypatch.setattr("app.worker.SessionLocal", lambda: db)
    monkeypatch.setattr(
        "app.worker.lookup_link",
        lambda session, url: calls.append(url) or {"product_id": "refreshed"},
    )
    refresh_once()
    assert calls == [listing.target.url]
    db.get(Source, "public-page:flipkart").display_allowed = False
    db.commit()
    refresh_once()
    assert len(calls) == 1


def test_collector_stops_that_retailer_after_refusal(db, monkeypatch):
    make_due_listing(db, "123")
    make_due_listing(db, "456")
    calls = []

    def blocked(session, url):
        calls.append(url)
        raise LookupError("retailer_blocked", "Blocked")

    monkeypatch.setattr("app.worker.SessionLocal", lambda: db)
    monkeypatch.setattr("app.worker.lookup_link", blocked)
    refresh_once()
    assert len(calls) == 1
