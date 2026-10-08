"""Persistent, bounded collection coordination shared by HTTP requests and workers."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import now, utc
from app.models import CollectionAttempt, CollectionBudget, CollectionJob, Offer, Product, Source
from app.retail import LookupError, Target, store_info, target_for


def insert_missing(db: Session, model, values: dict, key: str):
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    db.execute(insert(model).values(**values).on_conflict_do_nothing(index_elements=[key]))


def public_source(db: Session, target: Target) -> Source:
    source_id = "public-page:" + target.retailer
    insert_missing(
        db,
        Source,
        {
            "id": source_id,
            "name": store_info(target)[0] + " product page",
            "kind": "public_page",
            "display_allowed": True,
            "history_allowed": True,
        },
        "id",
    )
    source = db.get(Source, source_id)
    if (
        not source.display_allowed
        or not source.history_allowed
        or (source.expires_at and utc(source.expires_at) <= now())
    ):
        raise LookupError(
            "source_disabled", "This source has been disabled. No price was recorded.", 403
        )
    return source


def reserve_requests(db: Session, target: Target, cost: int = 1, *, commit: bool = True):
    source = public_source(db, target)
    at = now()
    day = at.date().isoformat()  # Budgets reset at UTC midnight; timestamps remain explicit.
    insert_missing(
        db,
        CollectionBudget,
        {
            "source_id": source.id,
            "budget_day": day,
            "reserved_requests": 0,
        },
        "source_id",
    )
    # Conditional updates serialize competing HTTP/worker reservations on both databases.
    db.execute(
        update(CollectionBudget)
        .execution_options(synchronize_session="fetch")
        .where(
            CollectionBudget.source_id == source.id,
            CollectionBudget.budget_day != day,
        )
        .values(budget_day=day, reserved_requests=0)
    )
    reserved = db.execute(
        update(CollectionBudget)
        .execution_options(synchronize_session="fetch")
        .where(
            CollectionBudget.source_id == source.id,
            CollectionBudget.reserved_requests <= get_settings().collection_daily_budget - cost,
            or_(CollectionBudget.blocked_until.is_(None), CollectionBudget.blocked_until <= at),
        )
        .values(reserved_requests=CollectionBudget.reserved_requests + cost)
    ).rowcount
    if not reserved:
        if commit:
            db.commit()
        raise LookupError(
            "collection_limited",
            "This store's collection limit or retry cooldown is active. Try later.",
            429,
        )
    if commit:
        db.commit()


def cadence(job: CollectionJob | None) -> timedelta:
    active = (
        job and job.last_requested_at and utc(job.last_requested_at) > now() - timedelta(days=7)
    )
    settings = get_settings()
    return timedelta(
        hours=settings.collection_active_hours if active else settings.collection_stable_hours
    )


def track_offer(db: Session, offer: Offer, *, requested: bool = False):
    at = now()
    insert_missing(
        db,
        CollectionJob,
        {
            "offer_id": offer.id,
            "status": "queued",
            "next_run_at": at + timedelta(hours=get_settings().collection_stable_hours),
            "consecutive_failures": 0,
        },
        "offer_id",
    )
    job = db.get(CollectionJob, offer.id)
    if requested:
        job.last_requested_at = at
    if job.status == "queued":
        # Direct successful checks may satisfy a queued refresh but never steal a lease.
        job.last_success_at = offer.current_observed_at
        job.next_run_at = utc(offer.current_observed_at or at) + cadence(job)
        job.consecutive_failures = 0
        job.last_error_code = None


def touch_offer(db: Session, offer: Offer):
    insert_missing(
        db,
        CollectionJob,
        {
            "offer_id": offer.id,
            "status": "queued",
            "next_run_at": now(),
            "consecutive_failures": 0,
        },
        "offer_id",
    )
    job = db.get(CollectionJob, offer.id)
    job.last_requested_at = now()
    if job.status == "queued" and job.consecutive_failures == 0:
        earliest = utc(offer.current_observed_at or now()) + cadence(job)
        job.next_run_at = min(utc(job.next_run_at), earliest)
    db.commit()


def record_attempt(db: Session, target: Target, *, started_at, error_code: str | None = None):
    source = db.get(Source, "public-page:" + target.retailer)
    if source is None:
        return
    offer = db.scalar(select(Offer).where(Offer.product_url == target.url))
    db.add(
        CollectionAttempt(
            offer_id=offer.id if offer else None,
            source_id=source.id,
            product_url=target.url,
            started_at=started_at,
            completed_at=now(),
            outcome="failure" if error_code else "success",
            error_code=error_code,
        )
    )
    if error_code:
        if offer:
            job = db.get(CollectionJob, offer.id)
            if job and job.status == "queued":
                retry_job(db, job, error_code)
        cool_source(db, source.id, error_code)


def cool_source(db: Session, source_id: str, code: str):
    if code in {"retailer_blocked", "retailer_unavailable", "connection_failed"}:
        db.execute(
            update(CollectionBudget)
            .execution_options(synchronize_session="fetch")
            .where(CollectionBudget.source_id == source_id)
            .values(blocked_until=now() + timedelta(hours=get_settings().collection_cooldown_hours))
        )


def retry_job(db: Session, job: CollectionJob, code: str):
    job.consecutive_failures += 1
    job.last_error_code = code
    job.lease_token = job.lease_expires_at = None
    job.status = "paused" if code in {"variant_changed", "source_disabled"} else "queued"
    minutes = min(15 * 2 ** min(job.consecutive_failures - 1, 7), 24 * 60)
    job.next_run_at = now() + timedelta(minutes=minutes)
    cool_source(db, db.get(Offer, job.offer_id).source_id, code)


def bootstrap_jobs(db: Session):
    offers = db.scalars(
        select(Offer)
        .join(Product)
        .join(Source)
        .outerjoin(CollectionJob)
        .where(
            CollectionJob.offer_id.is_(None),
            Offer.product_url.is_not(None),
            Product.is_demo.is_(False),
            Source.kind == "public_page",
            Source.display_allowed.is_(True),
            Source.history_allowed.is_(True),
            or_(Source.expires_at.is_(None), Source.expires_at > now()),
        )
        .limit(1000)
    )
    for offer in offers:
        insert_missing(
            db,
            CollectionJob,
            {
                "offer_id": offer.id,
                "status": "queued",
                "consecutive_failures": 0,
                "last_success_at": offer.current_observed_at,
                "next_run_at": utc(offer.current_observed_at or now()) + cadence(None),
            },
            "offer_id",
        )
    db.commit()


def claim_job(db: Session) -> dict | None:
    at = now()
    due = or_(
        (CollectionJob.status == "queued") & (CollectionJob.next_run_at <= at),
        (CollectionJob.status == "running") & (CollectionJob.lease_expires_at <= at),
    )
    query = (
        select(CollectionJob)
        .join(Offer)
        .join(Product)
        .join(Source)
        .outerjoin(
            CollectionBudget,
            CollectionBudget.source_id == Source.id,
        )
        .where(
            due,
            Offer.product_url.is_not(None),
            Product.is_demo.is_(False),
            Source.kind == "public_page",
            Source.display_allowed.is_(True),
            Source.history_allowed.is_(True),
            or_(Source.expires_at.is_(None), Source.expires_at > at),
            or_(CollectionBudget.blocked_until.is_(None), CollectionBudget.blocked_until <= at),
            or_(
                CollectionBudget.source_id.is_(None),
                CollectionBudget.budget_day != at.date().isoformat(),
                CollectionBudget.reserved_requests < get_settings().collection_daily_budget,
            ),
        )
        .order_by(CollectionJob.next_run_at, CollectionJob.offer_id)
        .limit(1)
    )
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True, of=CollectionJob)
    job = db.scalar(query)
    if job is None:
        db.rollback()
        return None
    token = str(uuid4())
    previous_token = job.lease_token
    claimed = db.execute(
        update(CollectionJob)
        .execution_options(synchronize_session="fetch")
        .where(
            CollectionJob.offer_id == job.offer_id,
            due,
        )
        .values(
            status="running",
            lease_token=token,
            lease_expires_at=at + timedelta(minutes=get_settings().collection_lease_minutes),
        )
    ).rowcount
    if not claimed:
        db.rollback()
        return None
    offer = db.get(Offer, job.offer_id)
    try:
        reserve_requests(db, target_for(offer.product_url), commit=False)
    except LookupError:
        db.rollback()
        return None
    if previous_token:
        db.execute(
            update(CollectionAttempt)
            .execution_options(synchronize_session="fetch")
            .where(
                CollectionAttempt.lease_token == previous_token,
                CollectionAttempt.outcome == "running",
            )
            .values(outcome="failure", error_code="lease_expired", completed_at=at)
        )
    attempt = CollectionAttempt(
        offer_id=offer.id,
        source_id=offer.source_id,
        product_url=offer.product_url,
        lease_token=token,
        started_at=at,
    )
    db.add(attempt)
    db.commit()
    return {
        "offer_id": offer.id,
        "url": offer.product_url,
        "token": token,
        "attempt_id": attempt.id,
    }
