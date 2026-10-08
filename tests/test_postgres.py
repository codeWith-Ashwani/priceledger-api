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
