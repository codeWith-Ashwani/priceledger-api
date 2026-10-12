"""Read-only owner visibility into collection health; never starts retailer requests."""

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.config import get_settings
from app.db import get_db, now
from app.evidence import approved_sources
from app.models import (
    CollectionAttempt,
    CollectionBudget,
    CollectionJob,
    Observation,
    Offer,
    Product,
    Retailer,
    Source,
)

router = APIRouter(prefix="/api/v1/admin/collection", dependencies=[Depends(require_admin)])
Status = Literal["all", "stale", "failed", "gap", "due", "paused", "unscheduled", "inactive"]


def source_active(at):
    return and_(
        Source.display_allowed.is_(True),
        Source.history_allowed.is_(True),
        or_(Source.expires_at.is_(None), Source.expires_at > at),
    )


def remaining_budget(at):
    maximum = get_settings().collection_daily_budget
    used = case(
        (CollectionBudget.budget_day == at.date().isoformat(), CollectionBudget.reserved_requests),
        else_=0,
    )
    return case((used >= maximum, 0), else_=maximum - used)


def offer_state(at):
    settings = get_settings()
    last_evidence = (
        select(Observation.observed_at)
        .join(Source, Observation.source_id == Source.id)
        .where(
            Observation.offer_id == Offer.id,
            Observation.observed_at <= at,
            Observation.item_paise.is_not(None),
            Observation.price_type == "listing",
            *approved_sources(at),
        )
        .order_by(Observation.observed_at.desc(), Observation.id.desc())
        .limit(1)
        .correlate(Offer)
        .scalar_subquery()
    )
    last_attempt_id = (
        select(CollectionAttempt.id)
        .where(
            CollectionAttempt.offer_id == Offer.id,
            CollectionAttempt.started_at <= at,
            CollectionAttempt.outcome != "running",
            or_(CollectionAttempt.completed_at.is_(None), CollectionAttempt.completed_at <= at),
        )
        .order_by(CollectionAttempt.started_at.desc(), CollectionAttempt.id.desc())
        .limit(1)
        .correlate(Offer)
        .scalar_subquery()
    )
    expired = and_(CollectionJob.status == "running", CollectionJob.lease_expires_at <= at)
    due = or_(and_(CollectionJob.status == "queued", CollectionJob.next_run_at <= at), expired)
    active = source_active(at)
    eligible = and_(
        active,
        remaining_budget(at) > 0,
        or_(CollectionBudget.blocked_until.is_(None), CollectionBudget.blocked_until <= at),
    )
    return (
        select(
            Offer.id.label("offer_id"),
            Offer.product_id,
            Product.title,
            Offer.retailer_id,
            Retailer.name.label("retailer_name"),
            Source.id.label("source_id"),
            Source.name.label("source_name"),
            Offer.current_observed_at,
            Offer.current_item_paise,
            Offer.availability,
            last_evidence.label("last_evidence_at"),
            CollectionJob.next_run_at,
            CollectionJob.status.label("job_status"),
            CollectionJob.consecutive_failures,
            CollectionJob.last_error_code,
            CollectionAttempt.outcome.label("last_check_outcome"),
            CollectionAttempt.error_code.label("last_check_error"),
            CollectionAttempt.started_at.label("last_check_at"),
            CollectionBudget.blocked_until,
            remaining_budget(at).label("remaining_requests"),
            active.label("source_active"),
            or_(
                Offer.current_item_paise.is_(None),
                Offer.current_observed_at.is_(None),
                Offer.current_observed_at > at,
                Offer.current_observed_at < at - timedelta(hours=settings.freshness_hours),
            ).label("stale"),
            or_(
                last_evidence.is_(None),
                last_evidence < at - timedelta(hours=settings.history_gap_hours),
            ).label("gap"),
            func.coalesce(
                or_(CollectionAttempt.outcome == "failure", CollectionJob.consecutive_failures > 0),
                False,
            ).label("failed"),
            func.coalesce(and_(due, eligible), False).label("due"),
            func.coalesce(expired, False).label("lease_expired"),
            func.coalesce(
                and_(CollectionJob.status == "running", CollectionJob.lease_expires_at > at), False
            ).label("running"),
            func.coalesce(CollectionJob.status == "paused", False).label("paused"),
            CollectionJob.offer_id.is_(None).label("unscheduled"),
            (~active).label("inactive"),
        )
        .select_from(Offer)
        .join(Product, Offer.product_id == Product.id)
        .join(Retailer, Offer.retailer_id == Retailer.id)
        .join(Source, Offer.source_id == Source.id)
        .outerjoin(CollectionJob, CollectionJob.offer_id == Offer.id)
        .outerjoin(CollectionBudget, CollectionBudget.source_id == Source.id)
        .outerjoin(CollectionAttempt, CollectionAttempt.id == last_attempt_id)
        .where(
            Product.is_demo.is_(False),
            Source.kind == "public_page",
            Offer.channel == "online",
            Offer.product_url.is_not(None),
        )
    )


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    at = now()
    state = offer_state(at).subquery()
    counters = ["stale", "gap", "failed", "due", "running", "paused", "unscheduled", "inactive"]
    summary = dict(
        db.execute(
            select(
                func.count().label("tracked"),
                *[
                    func.coalesce(func.sum(case((state.c[key].is_(True), 1), else_=0)), 0).label(
                        key
                    )
                    for key in counters
                ],
            ).select_from(state)
        )
        .mappings()
        .one()
    )
    checks = (
        db.execute(
            select(
                func.count().label("completed"),
                func.coalesce(
                    func.sum(case((CollectionAttempt.outcome == "failure", 1), else_=0)), 0
                ).label("failed"),
                func.max(CollectionAttempt.completed_at).label("latest_completed_at"),
            )
            .join(Source, CollectionAttempt.source_id == Source.id)
            .where(
                Source.kind == "public_page",
                CollectionAttempt.outcome != "running",
                CollectionAttempt.started_at <= at,
                func.coalesce(CollectionAttempt.completed_at, CollectionAttempt.started_at).between(
                    at - timedelta(hours=24), at
                ),
            )
        )
        .mappings()
        .one()
    )
    return {
        "checked_at": at,
        "summary": summary,
        "checks_24h": dict(checks),
        "freshness_hours": get_settings().freshness_hours,
        "gap_hours": get_settings().history_gap_hours,
        "note": "Counts describe recorded checks and scheduling, not whether a worker process is running. Gaps mean no approved listing-price observation within the configured interval. Stale includes missing prices.",
    }


@router.get("/offers")
def offers(
    status: Status = "all",
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    at = now()
    state = offer_state(at).subquery()
    filtered = select(state)
    if status != "all":
        filtered = filtered.where(state.c[status].is_(True))
    total = db.scalar(select(func.count()).select_from(filtered.subquery()))
    items = (
        db.execute(filtered.order_by(state.c.offer_id).offset(offset).limit(limit)).mappings().all()
    )
    return {
        "checked_at": at,
        "items": [dict(row) for row in items],
        "total": total,
        "limit": limit,
        "offset": offset,
        "status": status,
    }


@router.get("/sources")
def sources(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    at = now()
    counts = (
        select(Offer.source_id, func.count().label("tracked"))
        .join(Product, Offer.product_id == Product.id)
        .where(
            Product.is_demo.is_(False), Offer.channel == "online", Offer.product_url.is_not(None)
        )
        .group_by(Offer.source_id)
        .subquery()
    )
    attempts = (
        select(
            CollectionAttempt.source_id,
            func.count().label("completed"),
            func.sum(case((CollectionAttempt.outcome == "failure", 1), else_=0)).label("failed"),
            func.max(CollectionAttempt.completed_at).label("latest_completed_at"),
        )
        .where(
            CollectionAttempt.outcome != "running",
            CollectionAttempt.started_at <= at,
            func.coalesce(CollectionAttempt.completed_at, CollectionAttempt.started_at).between(
                at - timedelta(hours=24), at
            ),
        )
        .group_by(CollectionAttempt.source_id)
        .subquery()
    )
    total = db.scalar(select(func.count()).select_from(Source).where(Source.kind == "public_page"))
    rows = (
        db.execute(
            select(
                Source.id,
                Source.name,
                source_active(at).label("active"),
                Source.expires_at,
                CollectionBudget.blocked_until,
                remaining_budget(at).label("remaining_requests"),
                func.coalesce(counts.c.tracked, 0).label("tracked"),
                func.coalesce(attempts.c.completed, 0).label("completed_24h"),
                func.coalesce(attempts.c.failed, 0).label("failed_24h"),
                attempts.c.latest_completed_at,
            )
            .outerjoin(CollectionBudget, CollectionBudget.source_id == Source.id)
            .outerjoin(counts, counts.c.source_id == Source.id)
            .outerjoin(attempts, attempts.c.source_id == Source.id)
            .where(Source.kind == "public_page")
            .order_by(Source.name, Source.id)
            .offset(offset)
            .limit(limit)
        )
        .mappings()
        .all()
    )
    return {
        "checked_at": at,
        "items": [dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "daily_budget": get_settings().collection_daily_budget,
    }
