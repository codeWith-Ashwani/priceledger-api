"""Bounded price evidence and shared per-day source selection."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import now, utc
from app.models import Observation, Offer, Retailer, Source

INDIA = timezone(timedelta(hours=5, minutes=30))
PriceBasis = Literal["item", "delivered"]
QUERY_BATCH_SIZE = 100


def approved_sources(at: datetime | None = None):
    at = at or now()
    return [
        Source.history_allowed.is_(True),
        Source.display_allowed.is_(True),
        Source.expires_at.is_(None) | (Source.expires_at > at),
        Source.kind != "demo",
    ]


def observation_rows(
    db: Session, members: list[str], start: datetime, end: datetime, *, offer_id: str | None = None
) -> list:
    maximum = get_settings().max_history_observations
    rows = []
    for offset in range(0, len(members), QUERY_BATCH_SIZE):
        query = (
            select(Observation, Offer, Retailer, Source)
            .select_from(Observation)
            .join(Offer, Observation.offer_id == Offer.id)
            .join(Retailer, Offer.retailer_id == Retailer.id)
            .join(Source, Observation.source_id == Source.id)
            .where(
                Offer.product_id.in_(members[offset : offset + QUERY_BATCH_SIZE]),
                Observation.observed_at >= start,
                Observation.observed_at <= end,
                *approved_sources(end),
            )
            .order_by(Observation.observed_at, Observation.id)
            .limit(maximum + 1 - len(rows))
        )
        if offer_id is not None:
            query = query.where(Offer.id == offer_id)
        rows.extend(db.execute(query).all())
        if len(rows) > maximum:
            raise HTTPException(
                422, "This history range has too many observations. Choose a shorter period."
            )
    if len(members) <= QUERY_BATCH_SIZE:
        return rows  # The single SQL query already returns chronological order.
    return sorted(rows, key=lambda row: (utc(row[0].observed_at), row[0].id))


@dataclass(frozen=True)
class RecordedPrice:
    offer_id: str
    source_id: str
    retailer_name: str
    source_name: str
    at: datetime
    price_paise: int


def daily_evidence(
    rows: list, basis: PriceBasis, *, online_only: bool = True
) -> list[RecordedPrice]:
    grouped = defaultdict(lambda: defaultdict(list))
    own_sources = {}
    for observation, offer, retailer, source in rows:
        if (online_only and offer.channel != "online") or observation.price_type != "listing":
            continue
        if not observation.in_stock or observation.item_paise is None:
            continue
        if observation.evidence_kind == "historical_import" and (
            not observation.context_snapshot
            or any(
                observation.context_snapshot.get(key) != getattr(offer, key)
                for key in ("seller", "condition", "region")
            )
        ):
            continue
        price = observation.item_paise
        if basis == "delivered":
            if observation.shipping_paise is None:
                continue
            price += observation.shipping_paise
        at = utc(observation.observed_at)
        day = at.astimezone(INDIA).date()
        own_sources[offer.id] = offer.source_id
        grouped[(day, offer.id)][source.id].append(
            RecordedPrice(offer.id, source.id, retailer.name, source.name, at, price)
        )
    selected = []
    for (_, offer_id), sources in grouped.items():
        own = own_sources[offer_id]
        selected.extend(sources[own if own in sources else min(sources)])
    return selected
