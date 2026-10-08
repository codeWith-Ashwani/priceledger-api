from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import approved_sources, comparison_offers, get_product
from app.db import get_db, now, utc
from app.models import Observation, Source

router = APIRouter(prefix="/api/v1")


def assess(points: list[tuple[datetime, int]], current_paise: int | None, at: datetime) -> dict:
    reference_start, reference_end = at - timedelta(days=37), at - timedelta(days=7)
    daily: dict[str, list[int]] = defaultdict(list)
    for observed_at, amount in points:
        if reference_start <= observed_at < reference_end:
            daily[
                utc(observed_at)
                .astimezone(timezone(timedelta(hours=5, minutes=30)))
                .date()
                .isoformat()
            ].append(amount)
    baseline = (
        round(median([median(values) for values in daily.values()])) if len(daily) >= 20 else None
    )
    valid_points = [
        amount for timestamp, amount in points if at - timedelta(days=365) <= timestamp <= at
    ]
    recent = [amount for timestamp, amount in points if at - timedelta(days=14) <= timestamp <= at]
    saving = (
        round((baseline - current_paise) / baseline * 100, 1)
        if baseline and current_paise is not None
        else None
    )
    recent_high = max(recent, default=None)
    recent_rise = (
        baseline is not None
        and recent_high is not None
        and current_paise is not None
        and recent_high >= baseline * 1.10
        and current_paise < recent_high
    )
    verdict = "insufficient_history"
    if saving is not None:
        verdict = "below_usual" if saving >= 5 else "above_usual" if saving <= -5 else "near_usual"
    return {
        "verdict": verdict,
        "current_paise": current_paise,
        "baseline_paise": baseline,
        "savings_percent": saving,
        "recorded_low_paise": min(valid_points, default=None),
        "recorded_high_paise": max(valid_points, default=None),
        "recent_high_paise": recent_high,
        "recent_rise_then_drop": recent_rise,
        "reference_days_observed": len(daily),
        "reference_days_required": 20,
        "reference_start": reference_start,
        "reference_end": reference_end,
        "method": "Median of daily observed delivered prices over 30 days ending 7 days ago",
        "algorithm_version": "daily-median-v2-ist",
        "history_window_days": 365,
    }


@router.get("/products/{product_id}/assessment")
def assessment(product_id: str, db: Session = Depends(get_db)):
    get_product(db, product_id)
    offers = comparison_offers(db, product_id)
    eligible = [
        offer
        for offer in offers
        if offer["in_stock"] and not offer["stale"] and offer["total_paise"] is not None
    ]
    best = min(eligible, key=lambda offer: offer["total_paise"], default=None)
    points = []
    latest = None
    if best:
        observations = list(
            db.scalars(
                select(Observation)
                .join(Source)
                .where(
                    Observation.offer_id == best["id"],
                    Observation.observed_at >= now() - timedelta(days=365),
                    Observation.observed_at <= now(),
                    *approved_sources(),
                )
                .order_by(Observation.observed_at)
            )
        )
        for observation in observations:
            if (
                observation.in_stock
                and observation.item_paise is not None
                and observation.shipping_paise is not None
            ):
                points.append(
                    (
                        utc(observation.observed_at),
                        observation.item_paise + observation.shipping_paise,
                    )
                )
        latest = observations[-1] if observations else None
    result = assess(points, best["total_paise"] if best else None, now())
    result.update(
        {
            "offer_id": best["id"] if best else None,
            "retailer_name": best["retailer_name"] if best else None,
            "is_demo": best["is_demo"] if best else False,
            "advertised_discount_percent": round(
                (latest.mrp_paise - latest.item_paise) / latest.mrp_paise * 100, 1
            )
            if latest
            and latest.mrp_paise
            and latest.item_paise is not None
            and latest.mrp_paise >= latest.item_paise
            else None,
        }
    )
    return result
