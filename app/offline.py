"""Owner-reviewed, exact-product shop quotes; never modifies online evidence."""

import hashlib
import json
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import (
    UUID4,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.config import get_settings
from app.db import get_db, now, utc
from app.evidence import approved_sources
from app.limits import read_limit
from app.models import Offer, OfflineQuote, OfflineQuoteEvent, OfflineShop, Product, Source

router = APIRouter(prefix="/api/v1", dependencies=[Depends(read_limit)])
owner_router = APIRouter(prefix="/api/v1/admin/offline", dependencies=[Depends(require_admin)])
Condition = Literal["new", "used", "refurbished"]


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def location(value: str) -> str:
    return " ".join(value.casefold().split())


class ShopInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID4
    name: str = Field(min_length=2, max_length=120)
    city: str = Field(min_length=2, max_length=100)
    state: str = Field(min_length=2, max_length=100)
    area: str = Field(min_length=2, max_length=200)
    approval_reference: str = Field(min_length=5, max_length=1000)
    rights_confirmed: Literal[True]
    expires_at: AwareDatetime | None = None

    @field_validator("rights_confirmed", mode="before")
    @classmethod
    def explicit_rights(cls, value):
        if value is not True:
            raise ValueError("Confirm actual retention and display permission")
        return value


class QuoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID4
    product_id: str = Field(min_length=1, max_length=80)
    shop_id: UUID4
    variant_attributes: dict = Field(max_length=100)
    item_paise: StrictInt = Field(ge=0, le=2_000_000_000)
    condition: Condition
    in_stock: StrictBool
    observed_at: AwareDatetime
    valid_until: AwareDatetime
    evidence_reference: str = Field(min_length=5, max_length=1000)
    exact_variant_confirmed: Literal[True]
    includes_tax: Literal[True]
    unconditional_price: Literal[True]

    @field_validator(
        "exact_variant_confirmed", "includes_tax", "unconditional_price", mode="before"
    )
    @classmethod
    def explicit_evidence(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation from the shop evidence is required")
        return value

    @model_validator(mode="after")
    def valid_dates(self):
        if self.observed_at > now():
            raise ValueError("Quote time cannot be in the future")
        if not self.observed_at < self.valid_until <= self.observed_at + timedelta(days=30):
            raise ValueError("Validity must end after the quote, within 30 days")
        if len(json.dumps(self.variant_attributes, allow_nan=False)) > 10_000:
            raise ValueError("Variant attributes are too large")
        return self


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID4
    expected_revision: StrictInt = Field(ge=1)
    action: Literal["publish", "withdraw"]
    note: str = Field(min_length=5, max_length=1000)


def public_product(db: Session, product_id: str) -> Product:
    product = db.scalar(
        select(Product)
        .join(Offer)
        .join(Source)
        .where(
            Product.id == product_id,
            Product.is_demo.is_(False),
            Offer.channel == "online",
            *approved_sources(),
        )
        .limit(1)
    )
    if not product:
        raise HTTPException(404, "This product no longer has approved online identity evidence")
    return product


def shop_source(db: Session, shop_id: str):
    row = db.execute(
        select(OfflineShop, Source)
        .join(Source)
        .where(OfflineShop.id == shop_id, Source.kind == "merchant", *approved_sources())
    ).first()
    if not row:
        raise HTTPException(403, "The shop's display and retention permission is unavailable")
    return row


def quote_json(quote: OfflineQuote):
    return {
        "id": quote.id,
        "product_id": quote.product_id,
        "shop_id": quote.shop_id,
        "product_snapshot": quote.product_snapshot,
        "item_paise": quote.item_paise,
        "condition": quote.condition,
        "in_stock": quote.in_stock,
        "observed_at": utc(quote.observed_at),
        "valid_until": utc(quote.valid_until),
        "evidence_reference": quote.evidence_reference,
        "status": quote.status,
        "revision": quote.revision,
        "updated_at": utc(quote.updated_at),
    }


@owner_router.get("/shops")
def shops(
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(OfflineShop, Source)
        .join(Source)
        .order_by(OfflineShop.created_at.desc(), OfflineShop.id)
        .offset(offset)
        .limit(limit)
    )
    return {
        "items": [
            {
                "id": s.id,
                "name": s.name,
                "city": s.city,
                "state": s.state,
                "area": s.area,
                "source_active": source.kind == "merchant"
                and source.display_allowed
                and source.history_allowed
                and (not source.expires_at or utc(source.expires_at) > now()),
            }
            for s, source in rows
        ],
        "total": db.scalar(select(func.count()).select_from(OfflineShop)),
        "limit": limit,
        "offset": offset,
    }


@owner_router.post("/shops", status_code=201)
def create_shop(body: ShopInput, response: Response, db: Session = Depends(get_db)):
    values = body.model_dump(mode="json", exclude={"request_id"})
    if body.expires_at:
        values["expires_at"] = utc(body.expires_at).isoformat()
    fingerprint = digest(values)
    existing = db.get(OfflineShop, str(body.request_id))
    if existing:
        if existing.fingerprint != fingerprint:
            raise HTTPException(409, "This identifier belongs to a different shop submission")
        response.status_code = 200
        return {"id": existing.id, "replayed": True}
    if body.expires_at and body.expires_at <= now():
        raise HTTPException(422, "New source permission must expire in the future")
    shop_id = str(body.request_id)
    source_id = "offline-shop:" + shop_id
    source = Source(
        id=source_id,
        name=body.name + " shop evidence",
        kind="merchant",
        display_allowed=True,
        history_allowed=True,
        approval_reference=body.approval_reference,
        expires_at=body.expires_at,
    )
    shop = OfflineShop(
        id=shop_id,
        fingerprint=fingerprint,
        source_id=source_id,
        name=body.name,
        city=body.city,
        state=body.state,
        area=body.area,
        city_id=digest([location(body.city), location(body.state)])[:32],
        location_key=digest([location(x) for x in (body.name, body.city, body.state, body.area)]),
    )
    try:
        db.add(source)
        db.flush()
        db.add(shop)
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(OfflineShop, shop_id)
        if existing and existing.fingerprint == fingerprint:
            response.status_code = 200
            return {"id": existing.id, "replayed": True}
        raise HTTPException(409, "This shop or submission already exists") from None
    return {"id": shop_id, "replayed": False}


@owner_router.post("/quotes", status_code=201)
def create_quote(body: QuoteInput, response: Response, db: Session = Depends(get_db)):
    values = body.model_dump(mode="json", exclude={"request_id"})
    for key in ("observed_at", "valid_until"):
        values[key] = utc(getattr(body, key)).isoformat()
    fingerprint = digest(values)
    existing = db.get(OfflineQuote, str(body.request_id))
    if existing:
        if existing.fingerprint != fingerprint:
            raise HTTPException(409, "This identifier belongs to another quote")
        response.status_code = 200
        return {"quote": quote_json(existing), "replayed": True}
    product = public_product(db, body.product_id)
    shop_source(db, str(body.shop_id))
    if digest(product.attributes) != digest(body.variant_attributes):
        raise HTTPException(
            409, "The exact variant changed. Reload the product before recording its quote"
        )
    quote = OfflineQuote(
        id=str(body.request_id),
        fingerprint=fingerprint,
        product_id=product.id,
        shop_id=str(body.shop_id),
        product_snapshot={
            "title": product.title,
            "brand": product.brand,
            "attributes": product.attributes,
        },
        variant_digest=digest(product.attributes),
        item_paise=body.item_paise,
        condition=body.condition,
        in_stock=body.in_stock,
        observed_at=body.observed_at,
        valid_until=body.valid_until,
        evidence_reference=body.evidence_reference,
        status="draft",
        revision=1,
    )
    event = OfflineQuoteEvent(
        request_id=quote.id,
        quote_id=quote.id,
        fingerprint=fingerprint,
        revision=1,
        status="draft",
        note="Owner recorded a draft; evidence requires publication review",
    )
    try:
        db.add(quote)
        db.flush()
        db.add(event)
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(OfflineQuote, quote.id)
        if existing and existing.fingerprint == fingerprint:
            response.status_code = 200
            return {"quote": quote_json(existing), "replayed": True}
        raise HTTPException(409, "This quote submission already exists") from None
    return {"quote": quote_json(quote), "replayed": False}


@owner_router.get("/quotes")
def owner_quotes(
    status: Literal["all", "draft", "published", "withdrawn"] = "all",
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    conditions = [] if status == "all" else [OfflineQuote.status == status]
    query = select(OfflineQuote, OfflineShop).join(OfflineShop).where(*conditions)
    rows = db.execute(
        query.order_by(OfflineQuote.created_at.desc(), OfflineQuote.id).offset(offset).limit(limit)
    )
    return {
        "items": [
            {**quote_json(q), "shop_name": s.name, "city": s.city, "state": s.state}
            for q, s in rows
        ],
        "total": db.scalar(select(func.count()).select_from(OfflineQuote).where(*conditions)),
        "limit": limit,
        "offset": offset,
    }


@owner_router.get("/quotes/{quote_id}")
def owner_quote(
    quote_id: str,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    quote = db.get(OfflineQuote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    shop = db.get(OfflineShop, quote.shop_id)
    events = db.scalars(
        select(OfflineQuoteEvent)
        .where(OfflineQuoteEvent.quote_id == quote_id)
        .order_by(OfflineQuoteEvent.revision.desc())
        .offset(offset)
        .limit(limit)
    )
    return {
        "quote": {
            **quote_json(quote),
            "shop_name": shop.name,
            "city": shop.city,
            "state": shop.state,
            "area": shop.area,
        },
        "events": {
            "items": [
                {
                    "revision": e.revision,
                    "status": e.status,
                    "note": e.note,
                    "at": utc(e.created_at),
                }
                for e in events
            ],
            "total": db.scalar(
                select(func.count())
                .select_from(OfflineQuoteEvent)
                .where(OfflineQuoteEvent.quote_id == quote_id)
            ),
            "limit": limit,
            "offset": offset,
        },
    }


@owner_router.post("/quotes/{quote_id}/review")
def review_quote(quote_id: str, body: ReviewInput, db: Session = Depends(get_db)):
    fingerprint = digest(
        {"quote_id": quote_id, **body.model_dump(mode="json", exclude={"request_id"})}
    )
    event = db.get(OfflineQuoteEvent, str(body.request_id))
    if event:
        if event.fingerprint != fingerprint:
            raise HTTPException(409, "This review identifier belongs to another action")
        return {
            "id": quote_id,
            "status": event.status,
            "revision": event.revision,
            "replayed": True,
        }
    quote = db.get(OfflineQuote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    if (
        quote.revision != body.expected_revision
        or quote.status == "withdrawn"
        or (body.action == "publish" and quote.status != "draft")
    ):
        # A concurrent retry may commit between our receipt and quote reads.
        db.rollback()
        event = db.get(OfflineQuoteEvent, str(body.request_id))
        if event and event.fingerprint == fingerprint:
            return {
                "id": quote_id,
                "status": event.status,
                "revision": event.revision,
                "replayed": True,
            }
        raise HTTPException(409, "This quote changed or cannot make that transition. Reload it")
    if body.action == "publish":
        product = public_product(db, quote.product_id)
        shop_source(db, quote.shop_id)
        if digest(product.attributes) != digest(quote.product_snapshot["attributes"]):
            raise HTTPException(
                409, "The variant changed after the draft. Record and review a new quote"
            )
        if utc(quote.observed_at) > now():
            raise HTTPException(409, "Future quote evidence cannot be published")
    status = "published" if body.action == "publish" else "withdrawn"
    changes = {"status": status, "revision": body.expected_revision + 1, "updated_at": now()}
    if body.action == "publish":
        changes["published_at"] = now()
    changed = db.execute(
        update(OfflineQuote)
        .where(OfflineQuote.id == quote_id, OfflineQuote.revision == body.expected_revision)
        .values(**changes)
        .execution_options(synchronize_session=False)
    ).rowcount
    if not changed:
        db.rollback()
        event = db.get(OfflineQuoteEvent, str(body.request_id))
        if event and event.fingerprint == fingerprint:
            return {
                "id": quote_id,
                "status": event.status,
                "revision": event.revision,
                "replayed": True,
            }
        raise HTTPException(409, "This quote changed in another session. Reload it")
    db.add(
        OfflineQuoteEvent(
            request_id=str(body.request_id),
            quote_id=quote_id,
            fingerprint=fingerprint,
            revision=body.expected_revision + 1,
            status=status,
            note=body.note,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        event = db.get(OfflineQuoteEvent, str(body.request_id))
        if event and event.fingerprint == fingerprint:
            return {
                "id": quote_id,
                "status": event.status,
                "revision": event.revision,
                "replayed": True,
            }
        raise HTTPException(409, "This review conflicts with another action") from None
    return {
        "id": quote_id,
        "status": status,
        "revision": body.expected_revision + 1,
        "replayed": False,
    }


@router.get("/products/{product_id}/offline-quotes")
def public_quotes(
    product_id: str,
    city_id: str | None = Query(None, min_length=32, max_length=32, pattern=r"^[a-f0-9]+$"),
    condition: Condition = "new",
    days: int = Query(90, ge=1, le=3650),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    product = public_product(db, product_id)
    at = now()
    start = at - timedelta(days=days)
    policy = [
        OfflineQuote.product_id == product_id,
        OfflineQuote.condition == condition,
        OfflineQuote.variant_digest == digest(product.attributes),
        OfflineQuote.observed_at <= at,
        Source.kind == "merchant",
        *approved_sources(at),
    ]
    cities = [
        dict(row._mapping)
        for row in db.execute(
            select(
                OfflineShop.city_id,
                func.min(OfflineShop.city).label("city"),
                func.min(OfflineShop.state).label("state"),
            )
            .select_from(OfflineQuote)
            .join(OfflineShop)
            .join(Source)
            .where(*policy, OfflineQuote.status == "published", OfflineQuote.observed_at >= start)
            .group_by(OfflineShop.city_id)
            .order_by(func.min(OfflineShop.state), func.min(OfflineShop.city), OfflineShop.city_id)
        )
    ]
    selected = city_id or (cities[0]["city_id"] if cities else None)
    policy.append(OfflineShop.city_id == selected if selected else OfflineShop.id.is_(None))
    latest = select(
        OfflineQuote.id.label("latest_id"),
        OfflineQuote.shop_id,
        func.row_number()
        .over(
            partition_by=OfflineQuote.shop_id,
            order_by=(
                OfflineQuote.observed_at.desc(),
                OfflineQuote.created_at.desc(),
                OfflineQuote.id.desc(),
            ),
        )
        .label("rank"),
    )
    latest = (
        latest.select_from(OfflineQuote)
        .join(OfflineShop)
        .join(Source)
        .where(*policy, OfflineQuote.published_at.is_not(None))
        .subquery()
    )
    fresh_since = at - timedelta(days=get_settings().offline_freshness_days)
    current = (
        select(OfflineQuote.item_paise)
        .join(latest, OfflineQuote.id == latest.c.latest_id)
        .where(
            latest.c.rank == 1,
            OfflineQuote.status == "published",
            OfflineQuote.in_stock.is_(True),
            OfflineQuote.valid_until > at,
            OfflineQuote.observed_at >= fresh_since,
        )
    )
    eligible = current.subquery()
    count, low, high = db.execute(
        select(
            func.count(), func.min(eligible.c.item_paise), func.max(eligible.c.item_paise)
        ).select_from(eligible)
    ).one()
    history_policy = [
        *policy,
        OfflineQuote.status == "published",
        OfflineQuote.observed_at >= start,
    ]
    total = db.scalar(
        select(func.count())
        .select_from(OfflineQuote)
        .join(OfflineShop)
        .join(Source)
        .where(*history_policy)
    )
    rows = db.execute(
        select(OfflineQuote, OfflineShop, Source, latest.c.latest_id)
        .select_from(OfflineQuote)
        .join(OfflineShop)
        .join(Source)
        .join(latest, (OfflineQuote.shop_id == latest.c.shop_id) & (latest.c.rank == 1))
        .where(*history_policy)
        .order_by(OfflineQuote.observed_at.desc(), OfflineQuote.id.desc())
        .offset(offset)
        .limit(limit)
    )
    items = [
        {
            "id": q.id,
            "shop_id": shop.id,
            "shop_name": shop.name,
            "area": shop.area,
            "city": shop.city,
            "state": shop.state,
            "item_paise": q.item_paise,
            "condition": q.condition,
            "in_stock": q.in_stock,
            "observed_at": utc(q.observed_at),
            "valid_until": utc(q.valid_until),
            "reviewed_at": utc(q.updated_at),
            "source_name": source.name,
            "current": q.id == latest_id
            and q.in_stock
            and utc(q.valid_until) > at
            and utc(q.observed_at) >= fresh_since,
        }
        for q, shop, source, latest_id in rows
    ]
    return {
        "product_id": product_id,
        "read_at": at,
        "condition": condition,
        "city_id": selected,
        "cities": cities,
        "days": days,
        "freshness_days": get_settings().offline_freshness_days,
        "summary": {
            "current_shop_count": count,
            "range_ready": count >= 2,
            "low_paise": low if count >= 2 else None,
            "high_paise": high if count >= 2 else None,
        },
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
