from datetime import timedelta

from app.analysis import assess
from app.db import now


def test_returning_to_usual_price_is_not_a_real_saving():
    at = now()
    points = [(at - timedelta(days=day), 1_500_000) for day in range(8, 38)]
    points += [(at - timedelta(days=day), 1_800_000) for day in range(1, 7)]
    result = assess(points, 1_500_000, at)
    assert result["baseline_paise"] == 1_500_000
    assert result["savings_percent"] == 0
    assert result["recent_rise_then_drop"] is True
    assert result["verdict"] == "near_usual"


def test_poll_frequency_does_not_bias_daily_baseline():
    at = now()
    points = [(at - timedelta(days=day), 100_000) for day in range(8, 38)]
    points += [(at - timedelta(days=20, minutes=minute), 300_000) for minute in range(100)]
    assert assess(points, 90_000, at)["baseline_paise"] == 100_000


def test_insufficient_and_no_current_price_withhold_verdict():
    at = now()
    sparse = [(at - timedelta(days=day), 100_000) for day in range(8, 12)]
    assert assess(sparse, 90_000, at)["verdict"] == "insufficient_history"
    full = [(at - timedelta(days=day), 100_000) for day in range(8, 38)]
    assert assess(full, None, at)["savings_percent"] is None


def test_history_low_does_not_include_outside_window_or_future():
    at = now()
    result = assess(
        [(at - timedelta(days=400), 1), (at + timedelta(days=1), 2), (at, 300)], 300, at
    )
    assert result["recorded_low_paise"] == 300


def test_unknown_product_and_no_offers(client):
    assert client.get("/api/v1/products/missing/assessment").status_code == 404
    result = client.get("/api/v1/products/phone-256/assessment").json()
    assert result["verdict"] == "insufficient_history"
    assert result["current_paise"] is None


def test_assessment_endpoint_with_offer_but_no_history(client):
    result = client.get("/api/v1/products/phone-128/assessment")
    assert result.status_code == 200
    assert result.json()["current_paise"] == 10_500
    assert result.json()["retailer_name"] == "Test shop"
