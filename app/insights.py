"""Recorded exact-variant statistics; no inferred prices or purchase predictions."""

from collections import defaultdict
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.catalog import comparison_offers, get_product
from app.db import get_db, now
from app.evidence import INDIA, RecordedPrice, daily_evidence, observation_rows
from app.groups import member_ids
from app.limits import read_limit

router = APIRouter(prefix="/api/v1", dependencies=[Depends(read_limit)])


def summarize(
    records: list[RecordedPrice], product_id: str, days: int, basis: str, current: dict | None
) -> dict:
    daily = defaultdict(list)
    selected = []
    for record in records:
        daily[record.at.astimezone(INDIA).date()].append(record.price_paise)
        selected.append(
            {
                "price_paise": record.price_paise,
                "observed_at": record.at,
                "retailer_name": record.retailer_name,
                "source_name": record.source_name,
                "offer_id": record.offer_id,
            }
        )
    daily_lows = [min(prices) for prices in daily.values()]
    count = len(daily_lows)
    low = min(
        selected, key=lambda point: (point["price_paise"], point["observed_at"]), default=None
    )
    high = min(
        selected, key=lambda point: (-point["price_paise"], point["observed_at"]), default=None
    )
    return {
        "product_id": product_id,
        "days": days,
        "scope": "exact_variant",
        "price_basis": basis,
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
    get_product(db, product_id)
    members = member_ids(db, product_id)
    at = now()
    rows = observation_rows(db, members, at - timedelta(days=days), at)
    records = daily_evidence(rows, basis)
    eligible = [
        offer
        for offer in comparison_offers(db, product_id, members=members)
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
    return summarize(records, product_id, days, basis, current)
