from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.catalog import comparison_offers, get_product
from app.db import get_db, now, utc
from app.evidence import INDIA, daily_evidence, observation_rows
from app.limits import read_limit
from app.models import Offer

router = APIRouter(prefix="/api/v1", dependencies=[Depends(read_limit)])
REFERENCE_DAYS = 30
RECENT_EXCLUDED_DAYS = 7
MIN_REFERENCE_DAYS = 20
HISTORY_DAYS = 365
RECENT_PATTERN_DAYS = 14
SIGNIFICANT_SAVING_PERCENT = 5
RISE_THRESHOLD_RATIO = 1.10


def assess(points: list[tuple[datetime, int]], current_paise: int | None, at: datetime) -> dict:
    reference_end = at - timedelta(days=RECENT_EXCLUDED_DAYS)
    reference_start = reference_end - timedelta(days=REFERENCE_DAYS)
    daily: dict[str, list[int]] = defaultdict(list)
    for observed_at, amount in points:
        if reference_start <= observed_at < reference_end:
            daily[utc(observed_at).astimezone(INDIA).date().isoformat()].append(amount)
    baseline = (
        round(median([median(values) for values in daily.values()]))
        if len(daily) >= MIN_REFERENCE_DAYS
        else None
    )
    valid_points = [
        amount
        for timestamp, amount in points
        if at - timedelta(days=HISTORY_DAYS) <= timestamp <= at
    ]
    recent = [
        amount
        for timestamp, amount in points
        if at - timedelta(days=RECENT_PATTERN_DAYS) <= timestamp <= at
    ]
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
        and recent_high >= baseline * RISE_THRESHOLD_RATIO
        and current_paise < recent_high
    )
    verdict = "insufficient_history"
    if saving is not None:
        verdict = (
            "below_usual"
            if saving >= SIGNIFICANT_SAVING_PERCENT
            else "above_usual"
            if saving <= -SIGNIFICANT_SAVING_PERCENT
            else "near_usual"
        )
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
        "reference_days_required": MIN_REFERENCE_DAYS,
        "reference_start": reference_start,
        "reference_end": reference_end,
        "method": f"Median of daily observed delivered prices over {REFERENCE_DAYS} days ending {RECENT_EXCLUDED_DAYS} days ago",
        "algorithm_version": "daily-median-v3-source-ist",
        "history_window_days": HISTORY_DAYS,
    }


@router.get("/products/{product_id}/assessment")
def assessment(product_id: str, db: Session = Depends(get_db)):
    get_product(db, product_id)
    offers = comparison_offers(db, product_id)
    eligible = [
        offer
        for offer in offers
        if offer["channel"] == "online"
        and offer["in_stock"]
        and not offer["stale"]
        and offer["total_paise"] is not None
    ]
    best = min(eligible, key=lambda offer: offer["total_paise"], default=None)
    points = []
    latest = None
    at = now()
    if best:
        tracked = db.get(Offer, best["id"])
        rows = observation_rows(
            db, [tracked.product_id], at - timedelta(days=HISTORY_DAYS), at, offer_id=tracked.id
        )
        observations = [row[0] for row in rows]
        latest = next(
            (
                observation
                for observation in reversed(observations)
                if observation.source_id == tracked.source_id
                and tracked.current_observed_at
                and utc(observation.observed_at) == utc(tracked.current_observed_at)
            ),
            None,
        )
        points = [(record.at, record.price_paise) for record in daily_evidence(rows, "delivered")]
    result = assess(points, best["total_paise"] if best else None, at)
    result.update(
        {
            "history_source_policy": "One source per India day: own checks first, otherwise approved source ID order",
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
