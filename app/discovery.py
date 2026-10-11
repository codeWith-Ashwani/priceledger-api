"""Bounded discovery learned from genuine, readable Shopify product pages."""

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.collection import insert_missing
from app.config import get_settings
from app.db import get_db, now, utc
from app.models import DiscoveryStore, Source
from app.retail import (
    SHOPIFY_ORIGINS,
    STORES,
    Listing,
    LookupError,
    allowed_url,
    store_info,
    target_for,
)

router = APIRouter(prefix="/api/v1/admin/discovery-stores", dependencies=[Depends(require_admin)])


def learn_store(db: Session, listing: Listing):
    retailer = listing.target.retailer
    brand = listing.brand.strip()
    if (
        listing.discovery_kind != "shopify_html"
        or retailer in STORES
        or not brand
        or brand.lower() == "brand not stated"
    ):
        return
    host = urlsplit(allowed_url(listing.target.url, retailer)).hostname
    existing = db.get(DiscoveryStore, retailer)
    if existing is None:
        # Serialize new registrations without waiting on another catalog writer.
        # A busy registry can be learned on a subsequent real check instead.
        if db.bind.dialect.name == "postgresql" and not db.scalar(
            text("SELECT pg_try_advisory_xact_lock(1886156147, 12)")
        ):
            return
        if db.scalar(select(func.count()).select_from(DiscoveryStore)) >= 64:
            return
    insert_missing(
        db,
        DiscoveryStore,
        {
            "retailer_id": retailer,
            "source_id": "public-page:" + retailer,
            "host": host,
            "brands": [],
            "enabled": True,
            "learned_at": now(),
        },
        "retailer_id",
    )
    store = db.scalar(
        select(DiscoveryStore).where(DiscoveryStore.retailer_id == retailer).with_for_update()
    )
    if (
        store.enabled
        and brand.lower() not in {item.lower() for item in store.brands}
        and len(store.brands) < 32
    ):
        store.brands = [*store.brands, brand]


def registered_stores(db: Session, brand: str | None = None) -> dict[str, dict]:
    limit = get_settings().discovery_extra_store_limit
    if limit == 0:
        return {}
    rows = db.scalars(
        select(DiscoveryStore)
        .join(Source)
        .where(
            DiscoveryStore.enabled.is_(True),
            Source.kind == "public_page",
            Source.display_allowed.is_(True),
            Source.history_allowed.is_(True),
            Source.expires_at.is_(None) | (Source.expires_at > now()),
        )
        .order_by(DiscoveryStore.learned_at.desc(), DiscoveryStore.retailer_id)
        .limit(64)
    )
    result = {}
    for store in rows:
        if brand and brand.lower() not in {value.lower() for value in store.brands}:
            continue
        base = f"https://{store.host}/search?"
        # Revalidate persisted configuration before it can become a request.
        try:
            if target_for(base).retailer != store.retailer_id:
                continue
        except LookupError:
            continue
        result[store.retailer_id] = {"name": store_info(target_for(base))[0], "search_base": base}
        if limit > 0 and len(result) == limit:
            break
    # Start discovery at a small, brand-scoped set of readable public catalogs.
    # Never override an operator pause, revoked source or expired permission.
    for host, (name, store_brand) in SHOPIFY_ORIGINS.items():
        if (
            (limit > 0 and len(result) >= limit)
            or not brand
            or brand.lower() != store_brand.lower()
        ):
            continue
        base = f"https://{'www.' if host == 'imagineonline.store' else ''}{host}/search?"
        target = target_for(base)
        if target.retailer in result or db.get(DiscoveryStore, target.retailer) is not None:
            continue
        source = db.get(Source, "public-page:" + target.retailer)
        if source and (
            source.kind != "public_page"
            or not source.display_allowed
            or not source.history_allowed
            or (source.expires_at and utc(source.expires_at) <= now())
        ):
            continue
        result[target.retailer] = {"name": name, "search_base": base}
    return result


@router.get("")
def stores(db: Session = Depends(get_db)):
    rows = db.execute(
        select(DiscoveryStore, Source)
        .join(Source)
        .order_by(DiscoveryStore.learned_at.desc())
        .limit(64)
    )
    return [
        {
            "retailer_id": store.retailer_id,
            "host": store.host,
            "brands": store.brands,
            "enabled": store.enabled,
            "learned_at": store.learned_at,
            "source_active": source.kind == "public_page"
            and source.display_allowed
            and source.history_allowed
            and (source.expires_at is None or utc(source.expires_at) > now()),
        }
        for store, source in rows
    ]


class StoreStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


@router.post("/{retailer_id}/status")
def set_status(retailer_id: str, body: StoreStatus, db: Session = Depends(get_db)):
    store = db.get(DiscoveryStore, retailer_id)
    if store is None:
        raise HTTPException(404, "This learned store was not found.")
    store.enabled = body.enabled
    db.commit()
    return {"retailer_id": retailer_id, "enabled": store.enabled}
