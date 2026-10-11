"""Recorded exact-variant statistics; no inferred prices or purchase predictions."""

from collections import defaultdict
from datetime import timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import comparison_offers, history
from app.db import get_db, utc
from app.limits import read_limit
from app.models import Offer

router = APIRouter(prefix="/api/v1", dependencies=[Depends(read_limit)])
INDIA = timezone(timedelta(hours=5, minutes=30))


def summarize(recorded: dict, own_sources: dict[str, str], current: dict | None) -> dict:
    by_day = defaultdict(lambda: defaultdict(list))
    for series in recorded["series"]:
        if series["channel"] != "online" or series["price_type"] != "listing":
            continue
        for point in series["points"]:
            price = point["price_paise"]
            if not point["in_stock"] or price is None:
                continue
            at = utc(point["at"])
            key = (at.astimezone(INDIA).date().isoformat(), series["offer_id"])
            by_day[key][series["source_id"]].append(
                {
                    "price_paise": price,
                    "observed_at": at,
                    "retailer_name": series["name"],
                    "source_name": series["source_name"],
                    "offer_id": series["offer_id"],
                }
            )
    selected = []
    daily = defaultdict(list)
    for (day, offer_id), sources in by_day.items():
        own = own_sources.get(offer_id)
        chosen = own if own in sources else min(sources)
        points = sources[chosen]
        selected.extend(points)
        daily[day].extend(point["price_paise"] for point in points)
    daily_lows = [min(prices) for prices in daily.values()]
    count = len(daily_lows)
    low = min(
        selected, key=lambda point: (point["price_paise"], point["observed_at"]), default=None
    )
    high = min(
        selected, key=lambda point: (-point["price_paise"], point["observed_at"]), default=None
    )
    return {
        "product_id": recorded["product_id"],
        "days": recorded["days"],
        "scope": "exact_variant",
        "price_basis": recorded["price_basis"],
        "low": low,
        "high": high,
        "current": current,
        "daily_average_paise": (sum(daily_lows) + count // 2) // count if count else None,
        "observed_days": count,
        "observations": len(selected),
        "coverage_start": min((point["observed_at"] for point in selected), default=None),
        "coverage_end": max((point["observed_at"] for point in selected), default=None),
        "above_recorded_low_paise": current["price_paise"] - low["price_paise"]
        if current and low
        else None,
        "method": "Average of each India day's lowest observed available price across exact-variant online offers. Missing days are excluded.",
        "source_policy": "One source per offer per India day: own checks first, otherwise approved source ID order.",
        "history_note": "Recorded evidence within the selected period; not complete launch history or an all-time-low claim. Listed extras are excluded.",
    }


@router.get("/products/{product_id}/insights")
def insights(
    product_id: str,
    days: int = Query(default=90, ge=1, le=36500),
    basis: Literal["item", "delivered"] = "item",
    db: Session = Depends(get_db),
):
    recorded = history(product_id, days=days, basis=basis, db=db)
    ids = list({series["offer_id"] for series in recorded["series"]})
    own_sources = {}
    tracked = {}
    for offset in range(0, len(ids), 100):
        for offer in db.scalars(select(Offer).where(Offer.id.in_(ids[offset : offset + 100]))):
            own_sources[offer.id] = offer.source_id
            tracked[offer.id] = offer
    for series in recorded["series"]:
        offer = tracked[series["offer_id"]]
        series["points"] = [
            point
            for point in series["points"]
            if point["evidence_kind"] != "historical_import"
            or (
                point["context_snapshot"]
                and all(
                    point["context_snapshot"].get(key) == getattr(offer, key)
                    for key in ("seller", "condition", "region")
                )
            )
        ]
    eligible = [
        offer
        for offer in comparison_offers(db, product_id)
        if offer["channel"] == "online"
        and offer["in_stock"]
        and not offer["stale"]
        and offer["item_paise" if basis == "item" else "total_paise"] is not None
    ]
    best = min(
        eligible,
        key=lambda offer: offer["item_paise" if basis == "item" else "total_paise"],
        default=None,
    )
    current = (
        {
            "price_paise": best["item_paise" if basis == "item" else "total_paise"],
            "retailer_name": best["retailer_name"],
            "source_name": best["source_name"],
            "offer_id": best["id"],
            "observed_at": best["observed_at"],
        }
        if best
        else None
    )
    return summarize(recorded, own_sources, current)
