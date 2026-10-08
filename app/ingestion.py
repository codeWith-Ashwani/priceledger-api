import hashlib
import json

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import now, utc
from app.models import Observation, Offer, Product, Source
from app.schemas import IngestBatch, ObservationInput


class IngestionError(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


def ingest(
    db: Session, batch: IngestBatch, *, demo_mode: bool, commit: bool = True
) -> dict[str, int]:
    source = db.get(Source, batch.source_id)
    if not source or not source.history_allowed or not source.display_allowed:
        raise IngestionError("Source is not approved for display and history", 403)
    if source.expires_at and utc(source.expires_at) <= now():
        raise IngestionError("Source approval has expired", 403)
    if source.kind == "demo" and not demo_mode:
        raise IngestionError("Demo observations are disabled", 403)

    offer_ids = {record.offer_id for record in batch.observations}
    offers = {offer.id: offer for offer in db.scalars(select(Offer).where(Offer.id.in_(offer_ids)))}
    products = {
        product.id: product
        for product in db.scalars(
            select(Product).where(Product.id.in_({offer.product_id for offer in offers.values()}))
        )
    }
    event_ids = {record.external_event_id for record in batch.observations}
    existing = {
        record.external_event_id: record.fingerprint
        for record in db.scalars(
            select(Observation).where(
                Observation.source_id == source.id, Observation.external_event_id.in_(event_ids)
            )
        )
    }
    pending: dict[str, tuple] = {}
    duplicates = 0
    for record in batch.observations:
        offer = offers.get(record.offer_id)
        if not offer or offer.source_id != source.id:
            raise IngestionError("Offer does not belong to the approved source")
        canonical = record.model_dump(mode="json")
        # Preserve fingerprints of batches accepted before evidence labels were added.
        if record.evidence_kind == "recorded":
            canonical.pop("evidence_kind")
        canonical["observed_at"] = utc(record.observed_at).isoformat()
        fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
        previous = existing.get(record.external_event_id)
        if previous is None and record.external_event_id in pending:
            previous = pending[record.external_event_id][1]
        if previous is not None:
            if previous != fingerprint:
                raise IngestionError("Event ID was reused with different data", 409)
            duplicates += 1
            continue
        if json.dumps(record.variant_attributes, sort_keys=True, allow_nan=False) != json.dumps(
            products[offer.product_id].attributes, sort_keys=True, allow_nan=False
        ):
            raise IngestionError("Exact variant attributes do not match")
        pending[record.external_event_id] = (record, fingerprint, offer)

    try:
        latest: dict[str, ObservationInput] = {}
        for record, fingerprint, offer in pending.values():
            observed_at = utc(record.observed_at)
            db.add(
                Observation(
                    offer_id=offer.id,
                    source_id=source.id,
                    external_event_id=record.external_event_id,
                    fingerprint=fingerprint,
                    observed_at=observed_at,
                    item_paise=record.item_paise,
                    shipping_paise=record.shipping_paise,
                    mrp_paise=record.mrp_paise,
                    in_stock=record.in_stock,
                    currency=record.currency,
                    evidence_kind=record.evidence_kind,
                    variant_snapshot=dict(record.variant_attributes),
                    context_snapshot={
                        "seller": offer.seller,
                        "condition": offer.condition,
                        "region": offer.region,
                        "availability": offer.availability
                        if source.kind == "public_page"
                        else ("in_stock" if record.in_stock else "out_of_stock"),
                    }
                    if record.evidence_kind == "page_check"
                    else None,
                )
            )
            if offer.id not in latest or observed_at > utc(latest[offer.id].observed_at):
                latest[offer.id] = record
        db.flush()
        # Compare against persisted timestamps, not a possibly stale ORM object.
        # One conditional write per offer prevents delayed writers from replacing
        # a newer quote while keeping every genuine historical observation.
        for offer_id in sorted(latest):
            record = latest[offer_id]
            observed_at = utc(record.observed_at)
            db.execute(
                update(Offer)
                .where(
                    Offer.id == offer_id,
                    or_(
                        Offer.current_observed_at.is_(None), Offer.current_observed_at < observed_at
                    ),
                )
                .values(
                    current_observed_at=observed_at,
                    current_item_paise=record.item_paise,
                    current_shipping_paise=record.shipping_paise,
                    current_in_stock=record.in_stock,
                )
                .execution_options(synchronize_session="fetch")
            )
        if commit:
            db.commit()
        else:
            db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise IngestionError("Concurrent import conflict; retry the batch", 409) from exc
    return {"inserted": len(pending), "duplicates": duplicates}
