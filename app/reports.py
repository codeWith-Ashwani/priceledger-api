"""Public issue intake and private review; never rewrites price evidence."""

import hashlib
import json
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import (
    UUID4,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db, now, utc
from app.evidence import approved_sources
from app.limits import report_limit
from app.models import Offer, OfferReport, Product, ReportEvent, Retailer, Source

router = APIRouter(prefix="/api/v1")
owner_router = APIRouter(prefix="/api/v1/admin/reports", dependencies=[Depends(require_admin)])
Reason = Literal["wrong_price", "wrong_product", "outdated_price", "stock_status", "broken_link"]
Status = Literal["open", "in_review", "resolved", "dismissed"]


class ReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID4
    offer_id: str = Field(min_length=1, max_length=120)
    comparison_product_id: str = Field(min_length=1, max_length=80)
    reason: Reason
    details: str = Field(min_length=5, max_length=1000)
    observed_at: AwareDatetime | None
    item_paise: StrictInt | None = Field(ge=0, le=2_000_000_000)
    in_stock: StrictBool


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_revision: StrictInt = Field(ge=1)
    status: Status
    note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def resolution_note(self):
        if self.status in {"resolved", "dismissed"} and len(self.note) < 5:
            raise ValueError("Explain the resolution or dismissal in at least five characters")
        return self


def receipt(report: OfferReport, response: Response, *, replayed: bool):
    response.status_code = 200 if replayed else 201
    return {"report_id": report.id, "recorded_at": utc(report.created_at), "replayed": replayed}


def replay(db: Session, request_id: str, fingerprint: str, response: Response):
    existing = db.scalar(select(OfferReport).where(OfferReport.request_id == request_id))
    if existing:
        if existing.fingerprint != fingerprint:
            raise HTTPException(
                409, "This submission identifier already belongs to another report."
            )
        return receipt(existing, response, replayed=True)
    return None


@router.post("/reports", status_code=201, dependencies=[Depends(report_limit)])
def submit(body: ReportInput, response: Response, db: Session = Depends(get_db)):
    values = body.model_dump(mode="json", exclude={"request_id"})
    values["observed_at"] = utc(body.observed_at).isoformat() if body.observed_at else None
    fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    repeated = replay(db, str(body.request_id), fingerprint, response)
    if repeated is not None:
        return repeated
    row = db.execute(
        select(Offer, Product, Retailer, Source)
        .select_from(Offer)
        .join(Product, Offer.product_id == Product.id)
        .join(Retailer, Offer.retailer_id == Retailer.id)
        .join(Source, Offer.source_id == Source.id)
        .where(Offer.id == body.offer_id, Product.is_demo.is_(False), *approved_sources())
    ).first()
    comparison = db.scalar(
        select(Product)
        .join(Offer)
        .join(Source)
        .where(
            Product.id == body.comparison_product_id,
            Product.is_demo.is_(False),
            *approved_sources(),
        )
        .limit(1)
    )
    if not row or not comparison:
        raise HTTPException(
            404, "This offer or comparison product is no longer available for reporting."
        )
    offer, product, retailer, source = row
    observed_at = utc(offer.current_observed_at) if offer.current_observed_at else None
    if (
        observed_at != (utc(body.observed_at) if body.observed_at else None)
        or offer.current_item_paise != body.item_paise
        or offer.current_in_stock != body.in_stock
    ):
        raise HTTPException(
            409, "The offer changed since it was displayed. Refresh its prices before reporting."
        )
    at, report_id = now(), str(uuid4())
    snapshot = {
        "product_id": product.id,
        "title": product.title,
        "brand": product.brand,
        "variant_attributes": product.attributes,
        "retailer_name": retailer.name,
        "retailer_id": retailer.id,
        "source_id": source.id,
        "source_name": source.name,
        "product_url": offer.product_url,
        "channel": offer.channel,
        "item_paise": offer.current_item_paise,
        "shipping_paise": offer.current_shipping_paise,
        "observed_at": observed_at.isoformat() if observed_at else None,
        "in_stock": offer.current_in_stock,
        "availability": offer.availability,
        "seller": offer.seller,
        "condition": offer.condition,
        "region": offer.region,
        "comparison": {
            "product_id": comparison.id,
            "title": comparison.title,
            "brand": comparison.brand,
            "variant_attributes": comparison.attributes,
        },
    }
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    inserted = db.execute(
        insert(OfferReport)
        .values(
            id=report_id,
            request_id=str(body.request_id),
            fingerprint=fingerprint,
            offer_id=offer.id,
            reason=body.reason,
            details=body.details,
            snapshot=snapshot,
            status="open",
            revision=1,
            created_at=at,
            updated_at=at,
        )
        .on_conflict_do_nothing(index_elements=[OfferReport.request_id])
        .returning(OfferReport.id)
    ).scalar_one_or_none()
    if inserted is None:
        return replay(db, str(body.request_id), fingerprint, response)
    db.add(
        ReportEvent(
            report_id=report_id,
            revision=1,
            from_status=None,
            to_status="open",
            note="",
            created_at=at,
        )
    )
    db.commit()
    return receipt(db.get(OfferReport, report_id), response, replayed=False)


def report_json(report: OfferReport, *, details=False):
    data = {
        key: getattr(report, key)
        for key in (
            "id",
            "offer_id",
            "reason",
            "status",
            "revision",
            "created_at",
            "updated_at",
            "snapshot",
        )
    }
    for key in ("created_at", "updated_at"):
        data[key] = utc(data[key])
    if details:
        data["details"] = report.details
    return data


@owner_router.get("")
def queue(
    status: Literal["all", "open", "in_review", "resolved", "dismissed"] = "open",
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    query = select(OfferReport)
    if status != "all":
        query = query.where(OfferReport.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(
        query.order_by(OfferReport.created_at.desc(), OfferReport.id).offset(offset).limit(limit)
    )
    return {
        "items": [report_json(row) for row in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@owner_router.get("/{report_id}")
def detail(
    report_id: str,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    report = db.get(OfferReport, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    query = select(ReportEvent).where(ReportEvent.report_id == report_id)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    events = db.scalars(query.order_by(ReportEvent.revision.desc()).offset(offset).limit(limit))
    return {
        "report": report_json(report, details=True),
        "events": {
            "items": [
                {
                    **{
                        key: getattr(event, key)
                        for key in ("revision", "from_status", "to_status", "note")
                    },
                    "created_at": utc(event.created_at),
                }
                for event in events
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        },
    }


@owner_router.post("/{report_id}/review")
def review(report_id: str, body: ReviewInput, db: Session = Depends(get_db)):
    report = db.get(OfferReport, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    if report.revision != body.expected_revision:
        raise HTTPException(
            409, "This report changed in another session. Reload it before reviewing."
        )
    if report.status == body.status and not body.note:
        raise HTTPException(422, "Change the status or add a review note.")
    at, previous = now(), report.status
    changed = db.execute(
        update(OfferReport)
        .where(OfferReport.id == report_id, OfferReport.revision == body.expected_revision)
        .values(status=body.status, revision=body.expected_revision + 1, updated_at=at)
        .execution_options(synchronize_session=False)
    ).rowcount
    if not changed:
        db.rollback()
        raise HTTPException(
            409, "This report changed in another session. Reload it before reviewing."
        )
    db.add(
        ReportEvent(
            report_id=report_id,
            revision=body.expected_revision + 1,
            from_status=previous,
            to_status=body.status,
            note=body.note,
            created_at=at,
        )
    )
    db.commit()
    db.refresh(report)
    return report_json(report, details=True)
