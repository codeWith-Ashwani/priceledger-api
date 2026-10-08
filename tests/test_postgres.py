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
        {"sku": target.external_id},
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
