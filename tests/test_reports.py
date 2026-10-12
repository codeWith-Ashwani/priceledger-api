from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db import now
from app.models import (
    CollectionJob,
    Observation,
    Offer,
    OfferReport,
    Product,
    ProductMatch,
    ReportEvent,
    Source,
)


@pytest.fixture
def report_body(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "report_requests_per_minute", 60)
    offer = client.get("/api/v1/products/phone-128").json()["offers"][0]
    return {
        "request_id": str(uuid4()),
        "offer_id": offer["id"],
        "comparison_product_id": "phone-128",
        "reason": "wrong_price",
        "details": "The item price needs checking.",
        "observed_at": offer["observed_at"],
        "item_paise": offer["item_paise"],
        "in_stock": offer["in_stock"],
    }


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_token", "report-owner-test-only")
    return {"X-Admin-Token": "report-owner-test-only"}


def test_report_captures_evidence_without_changing_prices_matches_or_collection(
    client, db, report_body, owner
):
    response = client.post("/api/v1/reports", json=report_body)
    assert response.status_code == 201 and response.headers["Cache-Control"] == "no-store"
    receipt = response.json()
    assert set(receipt) == {"report_id", "recorded_at", "replayed"}
    assert receipt["recorded_at"].endswith("+00:00") and receipt["replayed"] is False
    report = db.get(OfferReport, receipt["report_id"])
    assert report.snapshot["item_paise"] == 10000 and report.snapshot["shipping_paise"] == 500
    assert report.snapshot["comparison"]["product_id"] == "phone-128"
    assert report.snapshot["variant_attributes"] == {"storage_gb": 128}
    for model in (Observation, ProductMatch, CollectionJob):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    db.get(Offer, "offer-128").current_item_paise = 12000
    db.get(Product, "phone-128").attributes = {"storage_gb": 256}
    db.commit()
    data = client.get(f"/api/v1/admin/reports/{report.id}", headers=owner).json()
    assert data["report"]["snapshot"]["item_paise"] == 10000
    assert data["report"]["snapshot"]["variant_attributes"] == {"storage_gb": 128}
    assert data["events"]["total"] == 1 and data["events"]["items"][0]["to_status"] == "open"


def test_retry_reuses_receipt_after_evidence_changes_but_changed_payload_conflicts(
    client, db, report_body
):
    original = client.post("/api/v1/reports", json=report_body).json()
    db.get(Source, "test").display_allowed = False
    db.get(Offer, "offer-128").current_item_paise = 12000
    db.commit()
    repeated = client.post("/api/v1/reports", json=report_body)
    assert repeated.status_code == 200 and repeated.json()["replayed"] is True
    assert repeated.json()["report_id"] == original["report_id"]
    changed = client.post(
        "/api/v1/reports", json={**report_body, "details": "A different report now."}
    )
    assert changed.status_code == 409
    assert db.scalar(select(func.count()).select_from(OfferReport)) == 1
    assert db.scalar(select(func.count()).select_from(ReportEvent)) == 1


@pytest.mark.parametrize(
    "change", [{"item_paise": 1}, {"in_stock": False}, {"observed_at": "2026-01-01T00:00:00Z"}]
)
def test_changed_displayed_quote_is_rejected_without_a_report(client, db, report_body, change):
    result = client.post("/api/v1/reports", json={**report_body, **change})
    assert result.status_code == 409 and "changed" in result.text
    assert db.scalar(select(func.count()).select_from(OfferReport)) == 0


@pytest.mark.parametrize("restriction", ["display", "history", "expired", "demo", "comparison"])
def test_hidden_or_missing_context_cannot_be_reported(client, db, report_body, restriction):
    if restriction in {"display", "history"}:
        setattr(db.get(Source, "test"), restriction + "_allowed", False)
    elif restriction == "expired":
        db.get(Source, "test").expires_at = now() - timedelta(seconds=1)
    elif restriction == "demo":
        db.get(Product, "phone-128").is_demo = True
    else:
        report_body["comparison_product_id"] = "phone-256"
    db.commit()
    assert client.post("/api/v1/reports", json=report_body).status_code == 404
    assert db.scalar(select(func.count()).select_from(OfferReport)) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": "not-a-uuid"},
        {"reason": "other"},
        {"details": " "},
        {"details": "x" * 1001},
        {"item_paise": True},
        {"in_stock": "true"},
        {"observed_at": "2026-10-01T10:00:00"},
        {"snapshot": {"price": 1}},
        {"email": "private@example.org"},
    ],
)
def test_submission_validation_is_bounded_strict_and_does_not_echo_values(
    client, report_body, change
):
    result = client.post("/api/v1/reports", json={**report_body, **change})
    assert result.status_code == 422
    assert "private@example.org" not in result.text and "not-a-uuid" not in result.text


def test_unpriced_offer_and_distinct_comparison_context_are_preserved(client, db, report_body):
    db.get(Offer, "offer-128").current_item_paise = None
    db.get(Offer, "offer-128").current_in_stock = False
    db.add(
        Offer(
            id="offer-256",
            product_id="phone-256",
            retailer_id="shop",
            source_id="test",
            external_id="context-256",
            seller="Merchant",
            current_item_paise=20000,
            current_in_stock=True,
            current_observed_at=now(),
        )
    )
    db.commit()
    report_body.update(
        item_paise=None, in_stock=False, comparison_product_id="phone-256", reason="wrong_product"
    )
    result = client.post("/api/v1/reports", json=report_body)
    assert result.status_code == 201
    snapshot = db.get(OfferReport, result.json()["report_id"]).snapshot
    assert snapshot["item_paise"] is None and snapshot["comparison"]["variant_attributes"] == {
        "storage_gb": 256
    }


def test_reporting_quota_is_separate_shared_and_ignores_forwarded_identity(
    client, report_body, monkeypatch
):
    monkeypatch.setattr(get_settings(), "report_requests_per_minute", 1)
    assert client.post("/api/v1/reports", json=report_body).status_code == 201
    result = client.post(
        "/api/v1/reports", json=report_body, headers={"X-Forwarded-For": "203.0.113.7"}
    )
    assert result.status_code == 429 and int(result.headers["Retry-After"]) > 0
    assert client.get("/api/v1/products/phone-128").status_code == 200


def test_review_requires_owner_and_version_and_keeps_notes_private(client, db, report_body, owner):
    report_id = client.post("/api/v1/reports", json=report_body).json()["report_id"]
    path = f"/api/v1/admin/reports/{report_id}"
    assert client.get("/api/v1/admin/reports").status_code == 401
    assert client.get(path).status_code == 401
    assert client.post(path + "/review", json={}).status_code == 401
    assert client.get(f"/api/v1/reports/{report_id}").status_code == 404
    assert client.get("/api/v1/reports").status_code == 405
    resolution = {
        "expected_revision": 1,
        "status": "resolved",
        "note": "Verified the source and recorded the investigation.",
    }
    result = client.post(path + "/review", json=resolution, headers=owner)
    assert result.status_code == 200 and result.json()["revision"] == 2
    assert result.json()["updated_at"].endswith("+00:00")
    conflict = client.post(
        path + "/review", json={**resolution, "status": "dismissed"}, headers=owner
    )
    assert conflict.status_code == 409 and "Reload" in conflict.text
    replay = client.post("/api/v1/reports", json=report_body)
    assert resolution["note"] not in replay.text and "snapshot" not in replay.text
    audit = client.get(path, headers=owner).json()["events"]
    assert audit["total"] == 2 and audit["items"][0]["from_status"] == "open"
    assert audit["items"][0]["to_status"] == "resolved"
    assert audit["items"][0]["created_at"].endswith("+00:00")
    assert db.get(Offer, "offer-128").current_item_paise == 10000
    assert db.scalar(select(func.count()).select_from(ProductMatch)) == 0


@pytest.mark.parametrize(
    "body",
    [
        {"status": "resolved", "note": ""},
        {"status": "dismissed", "note": "no"},
        {"status": "open", "note": ""},
        {"status": "invalid", "note": "test note"},
        {"status": "in_review", "expected_revision": True},
    ],
)
def test_invalid_or_empty_reviews_leave_the_audit_unchanged(client, db, report_body, owner, body):
    report_id = client.post("/api/v1/reports", json=report_body).json()["report_id"]
    result = client.post(
        f"/api/v1/admin/reports/{report_id}/review",
        json={"expected_revision": 1, **body},
        headers=owner,
    )
    assert result.status_code == 422
    assert db.get(OfferReport, report_id).revision == 1
    assert db.scalar(select(func.count()).select_from(ReportEvent)) == 1


def test_queue_and_audit_pagination_and_status_filters(client, db, report_body, owner):
    report_id = client.post("/api/v1/reports", json=report_body).json()["report_id"]
    original = db.get(OfferReport, report_id)
    for index in range(30):
        db.add(
            OfferReport(
                id=str(uuid4()),
                request_id=str(uuid4()),
                fingerprint="a" * 64,
                offer_id=original.offer_id,
                reason=original.reason,
                details="Controlled pagination contract",
                snapshot=original.snapshot,
                status="dismissed" if index % 2 else "open",
                revision=1,
            )
        )
    db.commit()
    data = client.get("/api/v1/admin/reports?status=all&limit=10&offset=20", headers=owner).json()
    assert data["total"] == 31 and len(data["items"]) == 10
    assert "details" not in data["items"][0] and "fingerprint" not in data["items"][0]
    assert client.get("/api/v1/admin/reports?status=dismissed", headers=owner).json()["total"] == 15
    assert client.get("/api/v1/admin/reports?status=invalid", headers=owner).status_code == 422
    assert client.get("/api/v1/admin/reports?limit=101", headers=owner).status_code == 422
    for revision in range(1, 4):
        result = client.post(
            f"/api/v1/admin/reports/{report_id}/review",
            json={
                "expected_revision": revision,
                "status": "in_review",
                "note": f"Investigation note {revision}",
            },
            headers=owner,
        )
        assert result.status_code == 200
    audit = client.get(f"/api/v1/admin/reports/{report_id}?limit=2&offset=2", headers=owner).json()[
        "events"
    ]
    assert audit["total"] == 4 and [row["revision"] for row in audit["items"]] == [2, 1]
    assert client.get("/api/v1/admin/reports/missing", headers=owner).status_code == 404
    assert (
        client.post(
            "/api/v1/admin/reports/missing/review",
            headers=owner,
            json={"expected_revision": 1, "status": "in_review"},
        ).status_code
        == 404
    )
