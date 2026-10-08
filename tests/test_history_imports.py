import csv
import io
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db import now
from app.ingestion import ingest
from app.models import HistoryImport, Observation, Offer, Product, Source, SourceOfferMapping
from app.schemas import IngestBatch, ObservationInput


@pytest.fixture
def admin(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_token", "import-test-only")
    client.headers["X-Admin-Token"] = "import-test-only"
    return client


@pytest.fixture
def archive(db):
    source = Source(
        id="archive",
        name="Approved archive contract",
        kind="historical_import",
        display_allowed=True,
        history_allowed=True,
        approval_reference="Isolated test contract",
    )
    db.add(source)
    db.commit()
    return source


def record(event="archive-1", **overrides):
    row = dict(
        offer_id="offer-128",
        retailer_id="shop",
        retailer_listing_id="external-128",
        external_event_id=event,
        observed_at=(now() - timedelta(days=10)).isoformat(),
        variant_attributes={"storage_gb": 128},
        seller="Merchant",
        condition="new",
        region="IN",
        item_paise=8000,
        shipping_paise=0,
        in_stock=True,
        evidence_reference="Contract record " + event,
    )
    row.update(overrides)
    return row


def preview(admin, *rows, source="archive", format="json", content=None):
    return admin.post(
        "/api/v1/admin/imports/preview",
        json={
            "source_id": source,
            "file_name": "actual-source-export." + format,
            "format": format,
            "content": content if content is not None else json.dumps(rows),
        },
    )


def commit(admin, result):
    return admin.post(f"/api/v1/admin/imports/{result['import_id']}/commit")


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_admin_routes_disabled_or_require_credentials(client, monkeypatch):
    for route in ("sources", "offers", "imports"):
        assert client.get("/api/v1/admin/" + route).status_code == 403
    monkeypatch.setattr(get_settings(), "admin_token", "import-test-only")
    assert client.get("/api/v1/admin/sources").status_code == 401
    assert client.post("/api/v1/admin/imports/preview", content="not json").status_code == 401


def test_preview_is_read_only_and_commit_replays_without_touching_live_prices(admin, db, archive):
    row = record(observed_at=(now() - timedelta(minutes=1)).isoformat())
    result = preview(admin, row, row).json()
    assert result["ready"] == 1 and result["duplicates"] == 1 and result["can_commit"]
    assert count(db, Observation) == 0 and count(db, SourceOfferMapping) == 0
    saved = commit(admin, result)
    assert saved.status_code == 200 and saved.json()["inserted"] == 1
    assert saved.json()["duplicates"] == 1
    assert commit(admin, result).json()["replayed"]
    assert count(db, Observation) == 1 and count(db, SourceOfferMapping) == 1
    offer = db.get(Offer, "offer-128")
    assert offer.current_item_paise == 10000 and offer.current_shipping_paise == 500
    observation = db.scalar(select(Observation))
    assert observation.source_id == "archive" and observation.evidence_kind == "historical_import"
    assert observation.context_snapshot["seller"] == "Merchant"
    assert observation.evidence_reference == row["evidence_reference"]
    assert observation.import_id == result["import_id"]
    repeated = preview(admin, row).json()
    assert repeated["ready"] == 0 and repeated["duplicates"] == 1
    assert commit(admin, repeated).json()["inserted"] == 0
    assert len(admin.get("/api/v1/admin/imports").json()) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"retailer_id": "other"},
        {"retailer_listing_id": "different"},
        {"variant_attributes": {"storage_gb": 256}},
        {"seller": "Other seller"},
        {"condition": "used"},
        {"region": "Other region"},
        {"currency": "USD"},
        {"price_type": "coupon"},
        {"item_paise": -1},
        {"item_paise": 3.5},
        {"item_paise": True},
        {"item_paise": "10000"},
        {"in_stock": "true"},
        {"observed_at": "2026-01-01"},
        {"observed_at": "2026-01-01T12:00:00"},
        {"observed_at": (now() + timedelta(days=1)).isoformat()},
        {"evidence_reference": ""},
        {"offer_id": "missing"},
        {"extra_unknown_field": "untrusted"},
    ],
)
def test_wrong_identity_or_invalid_evidence_blocks_entire_file(admin, db, archive, change):
    result = preview(admin, record("valid"), record("invalid", **change))
    assert result.status_code == 200
    assert result.json()["errors"] == 1 and not result.json()["can_commit"]
    assert result.json()["import_id"] is None
    assert count(db, Observation) == 0 and count(db, HistoryImport) == 0


def test_conflicting_event_is_rejected_and_no_partial_commit(admin, db, archive):
    original = preview(admin, record()).json()
    conflict = preview(admin, record(item_paise=7000), record("new-row")).json()
    assert commit(admin, original).status_code == 200
    assert commit(admin, conflict).status_code == 409
    assert count(db, Observation) == 1
    assert db.get(HistoryImport, conflict["import_id"]).status == "ready"
    again = preview(admin, record(item_paise=7000)).json()
    assert again["errors"] == 1 and "different" in again["rows"][0]["message"]


@pytest.mark.parametrize("change", ["variant", "context", "expiry", "permission", "draft_expiry"])
def test_preview_revalidates_mapping_and_source_at_commit(admin, db, archive, change):
    result = preview(admin, record()).json()
    if change == "variant":
        db.get(Product, "phone-128").attributes = {"storage_gb": 256}
    elif change == "context":
        db.get(Offer, "offer-128").seller = "Changed seller"
    elif change == "expiry":
        archive.expires_at = now() - timedelta(seconds=1)
    elif change == "permission":
        archive.history_allowed = False
    else:
        db.get(HistoryImport, result["import_id"]).expires_at = now() - timedelta(seconds=1)
    db.commit()
    assert commit(admin, result).status_code in {403, 409}
    assert count(db, Observation) == 0 and count(db, SourceOfferMapping) == 0


def test_source_approval_is_explicit_and_cannot_overwrite_existing_source(admin, db):
    body = {
        "id": "merchant-history",
        "name": "Merchant history",
        "approval_reference": "Merchant export agreement",
        "rights_confirmed": False,
    }
    assert admin.post("/api/v1/admin/sources", json=body).status_code == 422
    body["rights_confirmed"] = True
    assert admin.post("/api/v1/admin/sources", json=body).status_code == 201
    assert admin.post("/api/v1/admin/sources", json=body).status_code == 409
    assert any(
        source["approval_reference"] == body["approval_reference"]
        for source in admin.get("/api/v1/admin/sources").json()
    )
    assert db.get(Source, "merchant-history").display_allowed


def test_imported_history_remains_source_labelled_without_duplicate_store_ranking(
    admin, db, archive
):
    ingest(
        db,
        IngestBatch(
            source_id="test",
            observations=[
                ObservationInput(
                    offer_id="offer-128",
                    external_event_id="own-check",
                    observed_at=now() - timedelta(days=2),
                    variant_attributes={"storage_gb": 128},
                    item_paise=9000,
                    shipping_paise=0,
                    in_stock=True,
                )
            ],
        ),
        demo_mode=False,
    )
    assert commit(admin, preview(admin, record()).json()).status_code == 200
    history = admin.get("/api/v1/products/phone-128/history?days=365&basis=item").json()
    assert len(history["series"]) == 2
    assert len({series["series_id"] for series in history["series"]}) == 2
    assert {series["source_id"] for series in history["series"]} == {"test", "archive"}
    detail = admin.get("/api/v1/products/phone-128").json()
    assert len(detail["offers"]) == 1 and detail["best_price_paise"] == 10500
    coverage = admin.get("/api/v1/products/phone-128/coverage").json()["stores"][0]
    assert coverage["observations"] == 2 and coverage["imported_observations"] == 1
    assert len(coverage["history_sources"]) == 2
    # Revoking collection display does not revoke independently approved imported evidence.
    db.get(Source, "test").display_allowed = False
    db.commit()
    assert len(admin.get("/api/v1/products/phone-128/history").json()["series"]) == 1
    assert admin.get("/api/v1/products/phone-128/coverage").json()["stores"][0]["observations"] == 1
    archive.display_allowed = False
    db.commit()
    assert admin.get("/api/v1/products/phone-128/history").json()["series"] == []
    assert admin.get("/api/v1/products/phone-128/coverage").json()["stores"] == []


def test_csv_supports_bom_integer_paise_and_unknown_shipping(admin, db, archive):
    row = record(shipping_paise=None)
    row["variant_attributes"] = json.dumps(row["variant_attributes"])
    row["in_stock"] = "true"
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(row))
    writer.writeheader()
    writer.writerow(row)
    result = preview(admin, format="csv", content="\ufeff" + output.getvalue()).json()
    assert result["can_commit"]
    assert commit(admin, result).status_code == 200
    point = admin.get("/api/v1/products/phone-128/history?basis=delivered").json()["series"][0][
        "points"
    ][0]
    assert point["item_paise"] == 8000 and point["total_paise"] is None
    assert db.get(Offer, "offer-128").current_shipping_paise == 500


@pytest.mark.parametrize(
    "content,format",
    [
        ('[{"offer_id":"a","offer_id":"b"}]', "json"),
        ("[NaN]", "json"),
        ("{}", "json"),
        ("[]", "json"),
        ("offer_id,offer_id\na,b\n", "csv"),
        ("unknown_header\na\n", "csv"),
        ("offer_id,in_stock\na,true,extra\n", "csv"),
    ],
)
def test_malformed_files_are_rejected(admin, archive, content, format):
    assert preview(admin, format=format, content=content).status_code == 422


def test_limits_are_enforced_before_storing_any_rows(admin, db, archive):
    assert preview(admin, *[record(str(index)) for index in range(1001)]).status_code == 422
    assert preview(admin, content="x" * 1_000_001).status_code == 422
    assert admin.post("/api/v1/admin/imports/preview", content=b"x" * 1_500_001).status_code == 413
    assert count(db, Observation) == 0 and count(db, HistoryImport) == 0


def test_public_page_source_cannot_be_used_as_an_archive(admin, db):
    source = Source(
        id="public-page:shop",
        name="Shop page",
        kind="public_page",
        history_allowed=True,
        display_allowed=True,
    )
    db.add(source)
    db.commit()
    assert preview(admin, record(), source=source.id).status_code == 403


def test_overlapping_sources_do_not_bias_daily_reference(admin, db, archive, monkeypatch):
    at = now()
    monkeypatch.setattr("app.analysis.now", lambda: at)
    second = Source(
        id="z-archive",
        name="Second archive contract",
        kind="historical_import",
        history_allowed=True,
        display_allowed=True,
        approval_reference="Isolated test contract",
    )
    db.add(second)
    db.commit()
    rows = [
        record(f"a-{day}", observed_at=(at - timedelta(days=day)).isoformat(), item_paise=12000)
        for day in range(8, 38)
    ]
    assert commit(admin, preview(admin, *rows).json()).status_code == 200
    competing = [
        record(f"b-{day}", observed_at=(at - timedelta(days=day)).isoformat(), item_paise=100)
        for day in range(8, 38)
    ]
    assert commit(admin, preview(admin, *competing, source=second.id).json()).status_code == 200
    result = admin.get("/api/v1/products/phone-128/assessment").json()
    assert result["reference_days_observed"] == 30 and result["baseline_paise"] == 12000
    own = [
        ObservationInput(
            offer_id="offer-128",
            external_event_id=f"own-{day}",
            observed_at=at - timedelta(days=day),
            variant_attributes={"storage_gb": 128},
            item_paise=9000,
            shipping_paise=0,
            in_stock=True,
        )
        for day in range(8, 28)
    ]
    ingest(db, IngestBatch(source_id="test", observations=own), demo_mode=False)
    assert admin.get("/api/v1/products/phone-128/assessment").json()["baseline_paise"] == 9000


def test_boolean_variant_cannot_masquerade_as_numeric_attribute(admin, db, archive):
    db.get(Product, "phone-128").attributes = {"pack": 1}
    db.commit()
    result = preview(admin, record(variant_attributes={"pack": True})).json()
    assert result["errors"] == 1


def test_full_1000_row_batch_is_atomic_and_replayable(admin, db, archive):
    at = (now() - timedelta(days=30)).isoformat()
    rows = [record(f"bounded-{index}", observed_at=at) for index in range(1000)]
    reviewed = preview(admin, *rows).json()
    assert reviewed["ready"] == 1000 and reviewed["can_commit"]
    assert count(db, Observation) == 0
    saved = commit(admin, reviewed)
    assert saved.status_code == 200 and saved.json()["inserted"] == 1000
    assert count(db, Observation) == 1000 and count(db, SourceOfferMapping) == 1
    assert commit(admin, reviewed).json()["replayed"]
    assert count(db, Observation) == 1000
