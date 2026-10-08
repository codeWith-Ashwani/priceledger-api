import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="Requires the migrated and seeded PostgreSQL integration database",
)


def test_postgres_catalog_history_and_assessment_roundtrip():
    with TestClient(app) as client:
        catalog = client.get("/api/v1/products").json()
        assert catalog["total"] == 6
        detail = client.get("/api/v1/products/auralis-studio").json()
        assert detail["best_price_paise"] == 1_499_900
        assert len(detail["offers"]) == 3
        history = client.get("/api/v1/products/auralis-studio/history?days=365").json()
        assert len(history["series"]) == 3
        assert len(history["series"][0]["points"]) >= 360
        assessment = client.get("/api/v1/products/auralis-studio/assessment").json()
        assert assessment["reference_days_observed"] >= 20
        assert assessment["recent_rise_then_drop"] is True
