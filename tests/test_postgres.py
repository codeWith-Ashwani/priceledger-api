from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import SessionLocal, now
from app.ingestion import ingest
from app.main import app
from app.retail import Listing, Target
from app.schemas import IngestBatch, ObservationInput

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="Requires the migrated PostgreSQL integration database",
)


def test_postgres_lookup_history_and_assessment_roundtrip(monkeypatch):
    # Isolated CI contract fixture; no demo seed or external network dependency.
    target = Target(
        "flipkart",
        "https://www.flipkart.com/integration/p/itmPG123?pid=MOBPGTEST128",
        "MOBPGTEST128",
    )
    listing = Listing(
        target,
        "Integration Phone Blue 128 GB",
        "Integration",
        "mobile",
        {"sku": target.external_id, "gtin13": "1234567890128", "identity_category": "mobile"},
        None,
        3699900,
        0,
        "in_stock",
        "new",
        "Integration seller",
        {"value": 4.2, "best": 5.0, "count": 120},
    )
    monkeypatch.setattr("app.lookup.load_listing", lambda ignored: listing)
    with TestClient(app) as client:
        assert client.get("/api/v1/products").json()["total"] == 0
        result = client.post("/api/v1/lookup", json={"query": target.url})
        assert result.status_code == 200
        product_id = result.json()["product_id"]
        with SessionLocal() as db:
            ingest(
                db,
                IngestBatch(
                    source_id="public-page:flipkart",
                    observations=[
                        ObservationInput(
                            offer_id=product_id + ":public-page",
                            external_event_id=f"pg-history-{day}",
                            observed_at=now() - timedelta(days=day),
                            variant_attributes=listing.attributes,
                            item_paise=4500000,
                            shipping_paise=0,
                            in_stock=True,
                        )
                        for day in range(8, 38)
                    ],
                ),
                demo_mode=False,
            )
        detail = client.get(f"/api/v1/products/{product_id}").json()
        assert detail["best_price_paise"] == 3699900
        assert detail["is_demo"] is False
        assert detail["offers"][0]["product_url"] == target.url
        assert detail["offers"][0]["title"] == listing.title
        assert detail["offers"][0]["rating"]["value"] == 4.2
        history = client.get(f"/api/v1/products/{product_id}/history?days=365&basis=item").json()
        assert len(history["series"]) == 1
        assert len(history["series"][0]["points"]) == 31
        assessment = client.get(f"/api/v1/products/{product_id}/assessment").json()
        assert assessment["reference_days_observed"] >= 20
        assert assessment["verdict"] == "below_usual"
        assert client.post("/api/v1/lookup", json={"query": target.url}).json()["cached"]
        other = replace(
            listing,
            target=Target("amazon", "https://www.amazon.in/dp/B012345678", "B012345678"),
            item_paise=3999900,
        )
        monkeypatch.setattr(
            "app.comparison.discover_store",
            lambda retailer, query, expected=None: {
                "search": {"status": "ok", "search_url": "https://www.amazon.in/s?k=test"},
                "listings": [other] if retailer == "amazon" else [],
                "failures": [],
            },
        )
        comparison = client.post(f"/api/v1/products/{product_id}/compare")
        assert comparison.status_code == 200
        assert comparison.json()["stores"][0]["status"] == "verified"
        assert len(client.get(f"/api/v1/products/{product_id}").json()["offers"]) == 2
        assert len(client.get(f"/api/v1/products/{product_id}/history").json()["series"]) == 2


def test_postgres_exclusive_claims_and_atomic_budget(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import select

    from app.collection import claim_job, reserve_requests
    from app.lookup import store_listing
    from app.models import CollectionBudget, CollectionJob
    from app.retail import LookupError
    from app.worker import settle_job

    listings = []
    with SessionLocal() as db:
        for suffix in ("1", "2"):
            target = Target(
                "flipkart",
                f"https://www.flipkart.com/collector/p/itmPGCLAIM{suffix}?pid=MOBPGCLAIM{suffix}",
                f"MOBPGCLAIM{suffix}",
            )
            listing = Listing(
                target,
                "Collector integration",
                "Integration",
                "mobile",
                {"sku": target.external_id},
                None,
                10000,
                0,
                "in_stock",
                "new",
                "Integration",
            )
            product_id = store_listing(db, listing)
            db.get(CollectionJob, product_id + ":public-page").next_run_at = now() - timedelta(
                minutes=1
            )
            listings.append(listing)
        db.commit()
    # Hold one row lock while another session claims the other job without blocking.
    with SessionLocal() as locked:
        first_job = locked.scalar(
            select(CollectionJob)
            .where(CollectionJob.offer_id.like("flipkart:MOBPGCLAIM%"))
            .order_by(CollectionJob.offer_id)
            .limit(1)
            .with_for_update()
        )
        with SessionLocal() as other:
            claim2 = claim_job(other)
            assert claim2 and claim2["offer_id"] != first_job.offer_id
        locked.commit()
    with SessionLocal() as db:
        claim1 = claim_job(db)
        assert claim1 and claim1["offer_id"] != claim2["offer_id"]
        assert claim_job(db) is None
        by_id = {
            f"flipkart:{listing.target.external_id}:public-page": listing for listing in listings
        }
        assert settle_job(db, claim1, listing=by_id[claim1["offer_id"]])
        assert settle_job(db, claim2, listing=by_id[claim2["offer_id"]])
        budget = db.get(CollectionBudget, "public-page:flipkart")
        budget.reserved_requests = 0
        db.commit()
    monkeypatch.setattr(get_settings(), "collection_daily_budget", 3)

    def reserve(_):
        with SessionLocal() as db:
            try:
                reserve_requests(db, listings[0].target)
                return True
            except LookupError as exc:
                assert exc.code == "collection_limited"
                return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, range(8))) == 3
    with SessionLocal() as db:
        assert db.get(CollectionBudget, "public-page:flipkart").reserved_requests == 3
    with TestClient(app) as client:
        result = client.get("/api/v1/products/flipkart:MOBPGCLAIM1/coverage")
        assert result.status_code == 200
        assert result.json()["stores"][0]["observed_days"] == 1


def test_postgres_history_imports_are_atomic_and_replayable(monkeypatch):
    import json
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import func, select

    from app.models import HistoryImport, Observation, Offer, Product, SourceOfferMapping

    monkeypatch.setattr(get_settings(), "admin_token", "pg-import-test-only")
    with SessionLocal() as db:
        offer = db.get(Offer, "flipkart:MOBPGTEST128:public-page")
        product = db.get(Product, offer.product_id)
        row = {
            "offer_id": offer.id,
            "retailer_id": offer.retailer_id,
            "retailer_listing_id": offer.external_id,
            "external_event_id": "pg-archive-price",
            "observed_at": (now() - timedelta(days=60)).isoformat(),
            "variant_attributes": product.attributes,
            "seller": offer.seller,
            "condition": offer.condition,
            "region": offer.region,
            "item_paise": 4900000,
            "shipping_paise": 0,
            "in_stock": True,
            "evidence_reference": "PostgreSQL isolated historical contract",
        }
        original_price = offer.current_item_paise
    headers = {"X-Admin-Token": "pg-import-test-only"}
    with TestClient(app) as client:
        client.headers.update(headers)
        assert (
            client.post(
                "/api/v1/admin/sources",
                json={
                    "id": "pg-archive",
                    "name": "PostgreSQL archive contract",
                    "approval_reference": "Isolated integration contract",
                    "rights_confirmed": True,
                },
            ).status_code
            == 201
        )
        draft_ids = []
        for _ in range(2):
            preview = client.post(
                "/api/v1/admin/imports/preview",
                json={
                    "source_id": "pg-archive",
                    "file_name": "pg-export.json",
                    "format": "json",
                    "content": json.dumps([row]),
                },
            )
            assert preview.status_code == 200 and preview.json()["can_commit"]
            draft_ids.append(preview.json()["import_id"])

    # Two independent previews race to import the same event; one adds it, one skips it.
    def save(draft_id):
        with TestClient(app) as client:
            response = client.post(f"/api/v1/admin/imports/{draft_id}/commit", headers=headers)
            assert response.status_code == 200, response.text
            return response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, draft_ids))
    assert sum(result["inserted"] for result in results) == 1
    assert sum(result["duplicates"] for result in results) == 1
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Observation)
                .where(Observation.source_id == "pg-archive")
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(SourceOfferMapping)
                .where(SourceOfferMapping.source_id == "pg-archive")
            )
            == 1
        )
        assert db.get(Offer, row["offer_id"]).current_item_paise == original_price
        assert all(db.get(HistoryImport, id).status == "committed" for id in draft_ids)
    with TestClient(app) as client:
        assert client.post(f"/api/v1/admin/imports/{draft_ids[0]}/commit", headers=headers).json()[
            "replayed"
        ]
        history = client.get(
            "/api/v1/products/flipkart:MOBPGTEST128/history?days=365&basis=item"
        ).json()
        assert any(
            series["source_id"] == "pg-archive" and series["points"][0]["import_id"]
            for series in history["series"]
        )
        assert len(client.get("/api/v1/products/flipkart:MOBPGTEST128").json()["offers"]) == 2


def test_postgres_shared_quota_and_comparison_leases_are_atomic(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from uuid import uuid4

    from fastapi import HTTPException
    from sqlalchemy import select

    from app.coordination import claim_comparison, fence_comparison, release_comparison
    from app.limits import reserve_client
    from app.models import ClientQuota, ComparisonLease

    client_key = str(uuid4())
    at = now()
    monkeypatch.setattr("app.limits.now", lambda: at)

    def reserve(_):
        with SessionLocal() as db:
            try:
                reserve_client(db, client_key, "lookup", 5)
                return True
            except HTTPException as exc:
                assert exc.status_code == 429 and exc.headers["Retry-After"] == "60"
                return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(24))) == 5
    with SessionLocal() as db:
        import hashlib

        key = hashlib.sha256(client_key.encode()).hexdigest()
        assert db.get(ClientQuota, (key, "lookup")).requests == 5

    product_id = "flipkart:MOBPGTEST128"

    def claim(_):
        with SessionLocal() as db:
            try:
                return claim_comparison(db, product_id)
            except HTTPException as exc:
                assert exc.status_code == 409
                return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        winners = [token for token in pool.map(claim, range(16)) if token]
    assert len(winners) == 1
    with SessionLocal() as db:
        old_token = winners[0]
        lease = db.get(ComparisonLease, product_id)
        lease.expires_at = now() - timedelta(seconds=1)
        db.commit()
        new_token = claim_comparison(db, product_id)
        with pytest.raises(HTTPException) as expired:
            fence_comparison(db, product_id, old_token)
        assert expired.value.status_code == 409
        release_comparison(db, product_id, old_token)
        assert (
            db.scalar(select(ComparisonLease.token).where(ComparisonLease.product_id == product_id))
            == new_token
        )
        release_comparison(db, product_id, new_token)


def test_postgres_stale_quote_writer_preserves_newer_price():
    from uuid import uuid4

    from app.models import Offer, Product

    offer_id = "flipkart:MOBPGTEST128:public-page"
    at = now()
    with SessionLocal() as stale_db:
        stale = stale_db.get(Offer, offer_id)
        old_at = stale.current_observed_at
        with SessionLocal() as newer_db:
            newer = newer_db.get(Offer, offer_id)
            newer.current_observed_at, newer.current_item_paise = at, 3599900
            newer_db.commit()
        assert stale.current_observed_at == old_at
        product = stale_db.get(Product, stale.product_id)
        record = ObservationInput(
            offer_id=offer_id,
            external_event_id="pg-stale-" + str(uuid4()),
            observed_at=at - timedelta(seconds=10),
            variant_attributes=product.attributes,
            item_paise=3199900,
            shipping_paise=0,
            in_stock=True,
        )
        ingest(
            stale_db, IngestBatch(source_id=stale.source_id, observations=[record]), demo_mode=False
        )
        stale_db.refresh(stale)
        assert stale.current_item_paise == 3599900 and stale.current_observed_at == at


def test_postgres_readiness_and_database_timeouts():
    from sqlalchemy import text

    with TestClient(app) as client:
        assert client.get("/api/v1/ready").status_code == 200
    with SessionLocal() as db:
        settings = get_settings()
        for setting, expected in (
            ("statement_timeout", settings.database_statement_timeout_ms),
            ("lock_timeout", settings.database_lock_timeout_ms),
        ):
            milliseconds = db.scalar(
                text("SELECT extract(epoch FROM current_setting(:setting)::interval) * 1000"),
                {"setting": setting},
            )
            assert int(milliseconds) == expected


def test_postgres_opposite_catalog_scans_preserve_one_match():
    from concurrent.futures import ThreadPoolExecutor
    from uuid import uuid4

    from sqlalchemy import func, select

    from app.comparison import link_catalog_matches
    from app.lookup import store_listing
    from app.models import ProductMatch
    from app.retail import target_for

    brand = "Concurrency " + str(uuid4())
    listings = [
        Listing(
            target_for(f"https://concurrent-{name}.example.org/product/phone"),
            "Concurrent phone",
            brand,
            "mobile",
            {"gtin13": "1234567890128"},
            None,
            100000,
            0,
            "in_stock",
            "new",
            "Integration seller",
        )
        for name in ("first", "second")
    ]
    with SessionLocal() as db:
        ids = [store_listing(db, listing) for listing in listings]

    def scan(index):
        with SessionLocal() as db:
            link_catalog_matches(db, ids[index % 2], listings[index % 2])

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(scan, range(16)))
    left, right = sorted(ids)
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(ProductMatch)
                .where(
                    ProductMatch.left_id == left,
                    ProductMatch.right_id == right,
                )
            )
            == 1
        )


def test_postgres_learned_store_and_fee_json_roundtrip():
    from sqlalchemy import select

    from app.discovery import registered_stores
    from app.lookup import store_listing
    from app.models import DiscoveryStore, Observation, Offer
    from app.retail import target_for

    fee = {"label": "Listed handling fee", "amount_paise": 3900, "evidence": "page_price_breakdown"}
    listing = Listing(
        target_for("https://integration-shop.example.org/products/phone"),
        "Integration Phone",
        "Integration",
        "mobile",
        {"sku": "PGSHOP"},
        None,
        100000,
        0,
        "in_stock",
        "new",
        "Integration seller",
        listed_fees=[fee],
        discovery_kind="shopify_html",
    )
    with SessionLocal() as db:
        product_id = store_listing(db, listing)
        assert db.get(DiscoveryStore, listing.target.retailer).brands == ["Integration"]
        assert listing.target.retailer in registered_stores(db, "Integration")
        assert db.get(Offer, product_id + ":public-page").current_listed_fees == [fee]
        assert db.scalar(
            select(Observation).where(Observation.offer_id == product_id + ":public-page")
        ).listed_fees == [fee]


def test_postgres_concurrent_learning_cannot_exceed_registry_capacity():
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import func, select

    from app.lookup import store_listing
    from app.models import DiscoveryStore, Product, Source
    from app.retail import target_for

    with SessionLocal() as db:
        count = db.scalar(select(func.count()).select_from(DiscoveryStore))
        for index in range(63 - count):
            target = target_for(f"https://capacity-existing-{index}.example.org/products/phone")
            source_id = "public-page:" + target.retailer
            db.add(
                Source(
                    id=source_id,
                    name="Capacity integration source",
                    kind="public_page",
                    display_allowed=True,
                    history_allowed=True,
                )
            )
            db.flush()
            db.add(
                DiscoveryStore(
                    retailer_id=target.retailer,
                    source_id=source_id,
                    host=f"capacity-existing-{index}.example.org",
                    brands=["Integration"],
                    enabled=True,
                    learned_at=now(),
                )
            )
        db.commit()

    def register(index):
        listing = Listing(
            target_for(f"https://capacity-new-{index}.example.org/products/phone"),
            "Integration Phone",
            "Integration",
            "mobile",
            {"sku": "CAP"},
            None,
            100000,
            0,
            "in_stock",
            "new",
            "Integration seller",
            discovery_kind="shopify_html",
        )
        with SessionLocal() as db:
            return store_listing(db, listing)

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(register, range(8)))
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(DiscoveryStore)) == 64
        assert all(db.get(Product, product_id) is not None for product_id in ids)


def test_postgres_streamed_catalog_matching_preserves_every_batch():
    from sqlalchemy import select

    from app.comparison import link_catalog_matches, model_offers
    from app.lookup import store_listing
    from app.models import ProductMatch
    from app.retail import target_for

    # Exercise server-side cursor reads together with nested identity-link writes.
    anchor = Listing(
        target_for("https://coverage-anchor.example.org/products/contract"),
        "Coverage Integration Headphones Blue",
        "Coverage Integration",
        "electronics",
        {"model": "WH-520", "color": "Blue", "gtin13": "4006381333931"},
        None,
        100000,
        0,
        "in_stock",
        "new",
        "Integration seller",
    )
    with SessionLocal() as db:
        anchor_id = store_listing(db, anchor)
        ids = {
            store_listing(
                db,
                replace(
                    anchor,
                    target=target_for(
                        f"https://coverage-store-{index}.example.org/products/contract"
                    ),
                ),
                commit=False,
                manage_job=False,
            )
            for index in range(105)
        }
        db.commit()
        link_catalog_matches(db, anchor_id, anchor)
        pairs = list(
            db.scalars(
                select(ProductMatch).where(
                    (ProductMatch.left_id == anchor_id) | (ProductMatch.right_id == anchor_id)
                )
            )
        )
        assert len(pairs) == 105
        assert {
            pair.right_id if pair.left_id == anchor_id else pair.left_id for pair in pairs
        } == ids
        red = replace(
            anchor,
            target=target_for("https://coverage-red.example.org/products/contract"),
            title="Coverage Integration Headphones Red",
            attributes={**anchor.attributes, "color": "Red"},
        )
        red_id = store_listing(db, red)
        offers = model_offers(db, red_id, red)
        assert {offer["product_id"] for offer in offers} == ids | {anchor_id}
        from app.catalog import coverage
        from app.insights import insights

        recorded = coverage(anchor_id, db)
        assert len(recorded["stores"]) == 106
        assert all(store["observations"] == 1 for store in recorded["stores"])
        anchor_check = next(
            store for store in recorded["stores"] if store["offer_id"] == anchor_id + ":public-page"
        )
        assert anchor_check["last_check"]["outcome"] == "success"
        statistics = insights(anchor_id, days=90, basis="item", db=db)
        assert statistics["observations"] == 106
        assert statistics["low"]["price_paise"] == 100000
