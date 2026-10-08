from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db import now
from app.ingestion import ingest
from app.models import CollectionAttempt, Observation, Product, Source
from app.schemas import IngestBatch, ObservationInput


def record(event, at, **kwargs):
    return ObservationInput(
        offer_id="offer-128",
        external_event_id=event,
        observed_at=at,
        variant_attributes={"storage_gb": 128},
        item_paise=10000,
        shipping_paise=0,
        in_stock=True,
        **kwargs,
    )


def test_coverage_counts_india_days_and_retains_original_variant(client, db):
    day = now().date() - timedelta(days=10)
    at = datetime(day.year, day.month, day.day, 17, tzinfo=UTC)
    ingest(
        db,
        IngestBatch(
            source_id="test",
            observations=[
                record("early", at, evidence_kind="historical_import"),
                record("late", at + timedelta(hours=2), evidence_kind="historical_import"),
            ],
        ),
        demo_mode=False,
    )
    evidence = db.scalar(select(Observation))
    db.get(Product, "phone-128").attributes = {"storage_gb": 128, "color": "blue"}
    db.commit()
    assert evidence.variant_snapshot == {"storage_gb": 128}
    assert evidence.context_snapshot is None  # Do not assign today's seller to a historic quote.
    # Replaying an unchanged accepted event remains idempotent after metadata enrichment.
    assert (
        ingest(
            db,
            IngestBatch(
                source_id="test",
                observations=[record("early", at, evidence_kind="historical_import")],
            ),
            demo_mode=False,
        )["duplicates"]
        == 1
    )
    coverage = client.get("/api/v1/products/phone-128/coverage").json()
    store = coverage["stores"][0]
    assert store["observations"] == 2 and store["observed_days"] == 2
    assert store["imported_observations"] == 2 and not coverage["launch_history_verified"]
    db.get(Source, "test").display_allowed = False
    db.commit()
    assert client.get("/api/v1/products/phone-128/coverage").json()["stores"] == []


def test_failure_breaks_timeline_but_is_never_a_price(client, db):
    at = now() - timedelta(hours=4)
    ingest(
        db,
        IngestBatch(
            source_id="test",
            observations=[record("before", at), record("after", at + timedelta(hours=2))],
        ),
        demo_mode=False,
    )
    db.add(
        CollectionAttempt(
            offer_id="offer-128",
            source_id="test",
            product_url="https://example.com/product",
            started_at=at + timedelta(hours=1),
            completed_at=at + timedelta(hours=1),
            outcome="failure",
            error_code="connection_failed",
        )
    )
    db.commit()
    history = client.get("/api/v1/products/phone-128/history?basis=item").json()
    points = history["series"][0]["points"]
    assert len(points) == 2
    assert not points[0]["gap_before"] and points[1]["gap_before"]
    assert all(point["price_paise"] == 10000 for point in points)
    coverage = client.get("/api/v1/products/phone-128/coverage").json()["stores"][0]
    assert coverage["last_check"]["outcome"] == "failure"
    assert coverage["observations"] == 2
