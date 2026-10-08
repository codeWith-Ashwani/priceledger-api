"""Atomic per-client quotas shared by all API processes using the same database."""

import hashlib
from datetime import timedelta
from math import ceil

from fastapi import Depends, HTTPException, Request
from sqlalchemy import case, delete, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, now, utc
from app.models import ClientQuota


def reserve_client(db: Session, client: str, bucket: str, limit: int) -> None:
    at = now()
    key = hashlib.sha256(client.encode()).hexdigest()
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    statement = insert(ClientQuota).values(
        client_hash=key, bucket=bucket, requests=1, expires_at=at + timedelta(seconds=60)
    )
    expired = ClientQuota.expires_at <= at
    accepted = db.execute(
        statement.on_conflict_do_update(
            index_elements=[ClientQuota.client_hash, ClientQuota.bucket],
            set_={
                "requests": case((expired, 1), else_=ClientQuota.requests + 1),
                "expires_at": case(
                    (expired, statement.excluded.expires_at), else_=ClientQuota.expires_at
                ),
            },
            where=or_(expired, ClientQuota.requests < limit),
        ).returning(ClientQuota.expires_at)
    ).scalar_one_or_none()
    if accepted is not None:
        # The quota survives even if the subsequent lookup/validation fails.
        db.commit()
        return
    expires = db.scalar(
        select(ClientQuota.expires_at).where(
            ClientQuota.client_hash == key, ClientQuota.bucket == bucket
        )
    )
    db.rollback()
    retry = max(1, ceil((utc(expires) - at).total_seconds())) if expires else 60
    raise HTTPException(
        429,
        "Request limit reached. Wait before trying again.",
        headers={"Retry-After": str(retry)},
    )


def request_client(request: Request) -> str:
    # Forwarded headers are not trusted here. Configure Uvicorn's trusted proxy IPs explicitly.
    return request.client.host if request.client else "local"


def lookup_limit(request: Request, db: Session = Depends(get_db)):
    action = "comparison" if request.url.path.endswith("/compare") else "lookup"
    reserve_client(db, request_client(request), action, get_settings().lookup_requests_per_minute)


def read_limit(request: Request, db: Session = Depends(get_db)):
    reserve_client(db, request_client(request), "read", get_settings().read_requests_per_minute)


def admin_limit(request: Request, db: Session):
    reserve_client(db, request_client(request), "admin", get_settings().admin_requests_per_minute)


def prune_quotas(db: Session, max_rows: int = 1000) -> int:
    """Bounded housekeeping; retain expired windows for one day for operational inspection."""
    if not 1 <= max_rows <= 10_000:
        raise ValueError("Use 1–10,000 rows per cleanup pass")
    cutoff = now() - timedelta(days=1)
    keys = list(
        db.execute(
            select(ClientQuota.client_hash, ClientQuota.bucket)
            .where(ClientQuota.expires_at < cutoff)
            .order_by(ClientQuota.expires_at, ClientQuota.client_hash, ClientQuota.bucket)
            .limit(max_rows)
        )
    )
    deleted = (
        db.execute(
            delete(ClientQuota).where(
                tuple_(ClientQuota.client_hash, ClientQuota.bucket).in_(keys),
                ClientQuota.expires_at < cutoff,
            )
        ).rowcount
        if keys
        else 0
    )
    db.commit()
    return deleted
