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
            lambda retailer, query: {
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
