from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db import now, utc
from app.ingestion import IngestionError, ingest
from app.models import Observation, Offer, Source
from app.schemas import IngestBatch, ObservationInput


def record(event_id="event-1", **overrides):
    values = dict(offer_id="offer-128", external_event_id=event_id, observed_at=now(),
                  variant_attributes={"storage_gb": 128}, item_paise=9_000, shipping_paise=500, in_stock=True)
    values.update(overrides)
    return ObservationInput(**values)


def batch(*records, source="test"):
    return IngestBatch(source_id=source, observations=list(records))


def test_repeat_import_is_idempotent(db):
    payload = batch(record())
    assert ingest(db, payload, demo_mode=True) == {"inserted": 1, "duplicates": 0}
    assert ingest(db, payload, demo_mode=True) == {"inserted": 0, "duplicates": 1}
    assert db.scalar(select(func.count()).select_from(Observation)) == 1


def test_duplicate_in_one_batch_and_conflicting_payload(db):
    observation = record()
    assert ingest(db, batch(observation, observation), demo_mode=True)["inserted"] == 1
    with pytest.raises(IngestionError, match="reused"):
        ingest(db, batch(observation.model_copy(update={"item_paise": 8000})), demo_mode=True)


def test_older_backfill_does_not_replace_current_offer(db):
    ingest(db, batch(record(observed_at=now() - timedelta(days=60))), demo_mode=True)
    assert db.get(Offer, "offer-128").current_item_paise == 10_000


def test_exact_variant_and_approval_are_enforced(db):
    with pytest.raises(IngestionError, match="variant"):
        ingest(db, batch(record(variant_attributes={"storage_gb": 256})), demo_mode=True)
    with pytest.raises(IngestionError, match="approved"):
        ingest(db, batch(record(), source="blocked"), demo_mode=True)
    db.get(Source, "test").expires_at = now() - timedelta(seconds=1)
    db.commit()
    with pytest.raises(IngestionError, match="expired"):
        ingest(db, batch(record()), demo_mode=True)


def test_bad_batch_does_not_partially_import(db):
    with pytest.raises(IngestionError):
        ingest(db, batch(record(), record("event-2", offer_id="missing")), demo_mode=True)
    assert db.scalar(select(func.count()).select_from(Observation)) == 0


def test_unavailable_observation_is_a_gap_not_zero(client, db):
    observation = record(item_paise=None, shipping_paise=None, in_stock=False)
    ingest(db, batch(observation), demo_mode=True)
    response = client.get("/api/v1/products/phone-128/history").json()
    assert response["series"][0]["points"][0]["total_paise"] is None
    assert utc(db.get(Offer, "offer-128").current_observed_at) == observation.observed_at


def test_history_window_excludes_old_observations(client, db):
    ingest(db, batch(record("old", observed_at=now() - timedelta(days=100)), record("new")), demo_mode=True)
    history = client.get("/api/v1/products/phone-128/history", params={"days": 30}).json()
    assert len(history["series"][0]["points"]) == 1


def test_admin_ingestion_is_disabled_without_secret(client):
    assert client.post("/api/v1/ingest", json=batch(record()).model_dump(mode="json")).status_code == 403


def test_negative_naive_and_future_prices_are_rejected():
    for overrides in ({"item_paise": -1}, {"observed_at": now() + timedelta(hours=1)},
                      {"observed_at": now().replace(tzinfo=None)}, {"in_stock": True, "item_paise": None}):
        with pytest.raises(ValueError):
            record(**overrides)
