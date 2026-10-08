from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.collection import claim_job, reserve_requests
from app.config import get_settings
from app.db import now, utc
from app.lookup import lookup_link, store_listing
from app.models import (
    CollectionAttempt,
    CollectionBudget,
    CollectionJob,
    Observation,
    Offer,
    Source,
)
from app.retail import Listing, LookupError, Target
from app.worker import refresh_once, settle_job


def make_listing(db, key="123", *, due=True):
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
    if due:
        db.get(CollectionJob, product_id + ":public-page").next_run_at = now() - timedelta(
            minutes=1
        )
        db.commit()
    return listing


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def worker_sessions(db, monkeypatch):
    monkeypatch.setattr("app.worker.SessionLocal", sessionmaker(db.bind, expire_on_commit=False))


def test_worker_records_real_check_and_does_not_repeat_before_due(db, monkeypatch):
    listing = make_listing(db)
    worker_sessions(db, monkeypatch)
    calls = []
    monkeypatch.setattr("app.worker.load_listing", lambda target: calls.append(target) or listing)
    assert refresh_once() == 1
    assert refresh_once() == 0
    db.expire_all()
    job = db.get(CollectionJob, "flipkart:MOBTEST123:public-page")
    assert job.status == "queued" and job.lease_token is None
    assert utc(job.next_run_at) > now() + timedelta(hours=5)
    assert count(db, Observation) == 2
    assert count(db, CollectionAttempt) == 2
    assert len(calls) == 1
    assert db.get(CollectionBudget, "public-page:flipkart").reserved_requests == 1


def test_revoked_and_not_due_sources_are_not_fetched(db, monkeypatch):
    make_listing(db, due=False)
    worker_sessions(db, monkeypatch)
    monkeypatch.setattr("app.worker.load_listing", lambda _: pytest.fail("Unexpected network call"))
    assert refresh_once() == 0
    job = db.get(CollectionJob, "flipkart:MOBTEST123:public-page")
    job.next_run_at = now() - timedelta(days=1)
    db.get(Source, "public-page:flipkart").display_allowed = False
    db.commit()
    assert refresh_once() == 0


def test_refusal_cools_whole_source_preserves_prices_and_records_failure(db, monkeypatch):
    make_listing(db)
    make_listing(db, "456")
    worker_sessions(db, monkeypatch)
    calls = []

    def blocked(target):
        calls.append(target)
        raise LookupError("retailer_blocked", "Blocked")

    monkeypatch.setattr("app.worker.load_listing", blocked)
    assert refresh_once() == 1
    assert refresh_once() == 0
    db.expire_all()
    assert len(calls) == 1 and count(db, Observation) == 2
    failed = db.scalar(select(CollectionAttempt).where(CollectionAttempt.outcome == "failure"))
    assert failed.error_code == "retailer_blocked"
    assert db.get(Offer, failed.offer_id).current_item_paise == 10000
    assert db.get(CollectionJob, failed.offer_id).consecutive_failures == 1
    assert utc(db.get(CollectionBudget, failed.source_id).blocked_until) > now() + timedelta(
        hours=5
    )


def test_claim_is_exclusive_and_expired_worker_cannot_write(db):
    listing = make_listing(db)
    claim = claim_job(db)
    assert claim and claim_job(db) is None
    job = db.get(CollectionJob, claim["offer_id"])
    job.lease_expires_at = now() - timedelta(seconds=1)
    db.commit()
    assert settle_job(db, claim, listing=replace(listing, item_paise=99999)) is False
    replacement = claim_job(db)
    assert replacement and replacement["token"] != claim["token"]
    assert settle_job(db, claim, listing=replace(listing, item_paise=99999)) is False
    assert settle_job(db, replacement, listing=listing)
    assert count(db, Observation) == 2
    assert db.get(CollectionAttempt, claim["attempt_id"]).error_code == "lease_expired"
    assert db.get(Offer, claim["offer_id"]).current_item_paise == 10000


def test_changed_variant_pauses_collection_without_appending_a_price(db, monkeypatch):
    listing = make_listing(db)
    worker_sessions(db, monkeypatch)
    monkeypatch.setattr(
        "app.worker.load_listing", lambda _: replace(listing, attributes={"sku": "DIFFERENT"})
    )
    assert refresh_once() == 1
    db.expire_all()
    job = db.get(CollectionJob, "flipkart:MOBTEST123:public-page")
    assert job.status == "paused" and job.last_error_code == "variant_changed"
    assert count(db, Observation) == 1
    assert refresh_once() == 0


def test_budget_shared_with_lookup_resets_daily_but_preserves_cooldown(db, monkeypatch):
    listing = make_listing(db)
    monkeypatch.setattr(get_settings(), "collection_daily_budget", 1)
    reserve_requests(db, listing.target)
    with pytest.raises(LookupError, match="limit"):
        reserve_requests(db, listing.target)
    assert claim_job(db) is None
    budget = db.get(CollectionBudget, "public-page:flipkart")
    budget.budget_day = (now() - timedelta(days=1)).date().isoformat()
    budget.blocked_until = now() + timedelta(hours=1)
    db.commit()
    with pytest.raises(LookupError, match="cooldown"):
        reserve_requests(db, listing.target)
    budget.blocked_until = now() - timedelta(seconds=1)
    db.commit()
    reserve_requests(db, listing.target)
    assert budget.reserved_requests == 1


def test_cached_lookup_adds_no_duplicate_observation_or_network_budget(db, monkeypatch):
    listing = make_listing(db, due=False)
    monkeypatch.setattr(
        "app.lookup.load_listing", lambda _: pytest.fail("Cache must avoid network")
    )
    assert lookup_link(db, listing.target.url)["cached"]
    assert count(db, Observation) == 1 and count(db, CollectionAttempt) == 1
    assert count(db, CollectionBudget) == 0


def test_direct_failure_is_persisted_without_inventing_a_product(db, monkeypatch):
    target = Target("amazon", "https://www.amazon.in/dp/B012345678", "B012345678")

    def blocked(_):
        raise LookupError("retailer_blocked", "Blocked")

    monkeypatch.setattr("app.lookup.load_listing", blocked)
    with pytest.raises(LookupError):
        lookup_link(db, target.url)
    attempt = db.scalar(select(CollectionAttempt))
    assert attempt.offer_id is None and attempt.error_code == "retailer_blocked"
    assert count(db, Observation) == 0


def test_source_revocation_before_lookup_prevents_network(db, monkeypatch):
    listing = make_listing(db)
    db.get(Source, "public-page:flipkart").history_allowed = False
    db.commit()
    monkeypatch.setattr("app.lookup.load_listing", lambda _: pytest.fail("Revoked source fetched"))
    with pytest.raises(LookupError, match="disabled"):
        lookup_link(db, listing.target.url)
