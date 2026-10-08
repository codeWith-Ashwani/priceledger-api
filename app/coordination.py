"""Per-product comparison leases; no database transaction spans retailer network calls."""

from datetime import timedelta
from math import ceil
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import now, utc
from app.models import ComparisonLease


def claim_comparison(db: Session, product_id: str) -> str:
    at, token = now(), str(uuid4())
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    statement = insert(ComparisonLease).values(
        product_id=product_id,
        token=token,
        expires_at=at + timedelta(seconds=get_settings().comparison_lease_seconds),
    )
    claimed = db.execute(
        statement.on_conflict_do_update(
            index_elements=[ComparisonLease.product_id],
            set_={"token": token, "expires_at": statement.excluded.expires_at},
            where=ComparisonLease.expires_at <= at,
        ).returning(ComparisonLease.token)
    ).scalar_one_or_none()
    if claimed:
        db.commit()
        return token
    expires = db.scalar(
        select(ComparisonLease.expires_at).where(ComparisonLease.product_id == product_id)
    )
    db.rollback()
    retry = max(1, ceil((utc(expires) - at).total_seconds())) if expires else 1
    raise HTTPException(
        409,
        "This product comparison is already running. Try again shortly.",
        headers={"Retry-After": str(min(retry, 15))},
    )


def fence_comparison(db: Session, product_id: str, token: str):
    # Lock the active lease until the comparison cache commits; a reclaimed request
    # cannot publish an expired result over the newer owner's result.
    owned = db.execute(
        update(ComparisonLease)
        .where(
            ComparisonLease.product_id == product_id,
            ComparisonLease.token == token,
            ComparisonLease.expires_at > now(),
        )
        .values(token=token)
    ).rowcount
    if not owned:
        db.rollback()
        raise HTTPException(
            409, "Comparison lease expired. Retry the request.", headers={"Retry-After": "1"}
        )


def release_comparison(db: Session, product_id: str, token: str):
    db.rollback()
    db.execute(
        delete(ComparisonLease).where(
            ComparisonLease.product_id == product_id, ComparisonLease.token == token
        )
    )
    db.commit()
