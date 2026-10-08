"""Reviewable, bounded history imports with immutable source-to-listing mappings."""

import csv
import hashlib
import io
import json
import re
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db, now, utc
from app.models import HistoryImport, Observation, Offer, Product, Source, SourceOfferMapping

router = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(require_admin)])
MAX_BODY = 1_500_000
MAX_CONTENT = 1_000_000
MAX_ROWS = 1000


class ImportRow(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    offer_id: str = Field(min_length=1, max_length=120)
    retailer_id: str = Field(min_length=1, max_length=80)
    retailer_listing_id: str = Field(min_length=1, max_length=150)
    external_event_id: str = Field(min_length=1, max_length=160)
    observed_at: AwareDatetime
    variant_attributes: dict[str, str | int | float | bool] = Field(min_length=1, max_length=100)
    seller: str = Field(min_length=1, max_length=150)
    condition: str = Field(min_length=1, max_length=40)
    region: str = Field(min_length=1, max_length=80)
    item_paise: StrictInt | None = Field(default=None, ge=0, le=2_000_000_000)
    shipping_paise: StrictInt | None = Field(default=None, ge=0, le=2_000_000_000)
    mrp_paise: StrictInt | None = Field(default=None, ge=0, le=2_000_000_000)
    in_stock: StrictBool
    currency: Literal["INR"] = "INR"
    price_type: Literal["listing"] = "listing"
    evidence_reference: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def valid_quote(self):
        if self.observed_at > now():
            raise ValueError("Historical observations must be in the past")
        if self.in_stock and self.item_paise is None:
            raise ValueError("Available quotes require an item price")
        if len(json.dumps(self.variant_attributes, allow_nan=False)) > 10_000:
            raise ValueError("Variant attributes are too large")
        return self


class ImportFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=80)
    file_name: str = Field(min_length=1, max_length=150)
    format: Literal["csv", "json"]
    content: str = Field(min_length=1, max_length=MAX_CONTENT)


class SourceApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]+$")
    name: str = Field(min_length=3, max_length=200)
    approval_reference: str = Field(min_length=5, max_length=1000)
    expires_at: AwareDatetime | None = None
    rights_confirmed: Literal[True]

    @field_validator("rights_confirmed", mode="before")
    @classmethod
    def explicit_approval(cls, value):
        if value is not True:
            raise ValueError(
                "Confirm actual retention and display rights before registering the source"
            )
        return value


def permitted_source(db: Session, source_id: str) -> Source:
    source = db.get(Source, source_id)
    if (
        not source
        or not source.display_allowed
        or not source.history_allowed
        or source.kind in {"demo", "public_page"}
    ):
        raise HTTPException(
            403,
            "Select an approved historical/merchant source; public-page sources cannot impersonate an archive",
        )
    if source.expires_at and utc(source.expires_at) <= now():
        raise HTTPException(403, "The source approval has expired")
    return source


def strict_json(content: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON field: " + key)
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Non-finite JSON numbers are not accepted")

    return json.loads(content, object_pairs_hook=pairs, parse_constant=constant)


def parse_file(file: ImportFile) -> tuple[list, bool]:
    if len(file.content.encode("utf-8")) > MAX_CONTENT:
        raise HTTPException(413, "The file must be at most 1 MB")
    try:
        if file.format == "json":
            rows = strict_json(file.content.lstrip("\ufeff"))
            if not isinstance(rows, list):
                raise ValueError("JSON must contain an array of observation records")
        else:
            reader = csv.DictReader(io.StringIO(file.content.lstrip("\ufeff")), strict=True)
            fields = reader.fieldnames
            if (
                not fields
                or len(set(fields)) != len(fields)
                or any(field not in ImportRow.model_fields for field in fields)
            ):
                raise ValueError("CSV headers must be unique known observation fields")
            rows = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("CSV row length does not match its header")
                rows.append(row)
                if len(rows) > MAX_ROWS:
                    raise ValueError("Each file is limited to 1,000 records")
        if not 1 <= len(rows) <= MAX_ROWS:
            raise ValueError("Provide between 1 and 1,000 records")
        return rows, file.format == "csv"
    except (ValueError, csv.Error, RecursionError) as exc:
        raise HTTPException(422, str(exc)) from exc


def row_model(raw, is_csv: bool) -> ImportRow:
    if is_csv:
        raw = {key: value.strip() for key, value in raw.items()}
        for key in ("item_paise", "shipping_paise", "mrp_paise"):
            if key in raw:
                value = raw[key]
                if value and not re.fullmatch(r"[0-9]+", value):
                    raise ValueError(key + " must be integer paise, not rupees or a formula")
                raw[key] = int(value) if value else None
        if raw.get("in_stock", "").lower() not in {"true", "false"}:
            raise ValueError("in_stock must be true or false")
        raw["in_stock"] = raw["in_stock"].lower() == "true"
        raw["variant_attributes"] = strict_json(raw.get("variant_attributes", ""))
        for key in ("currency", "price_type"):
            if raw.get(key) == "":
                del raw[key]
    return ImportRow.model_validate(raw)


def fingerprint(row: ImportRow) -> str:
    value = row.model_dump(mode="json")
    value["observed_at"] = utc(row.observed_at).isoformat()
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def exact_attributes(left: dict, right: dict) -> bool:
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
        right, sort_keys=True, allow_nan=False
    )


def context(row: ImportRow) -> dict:
    return {"seller": row.seller, "condition": row.condition, "region": row.region}


def inspect_rows(
    db: Session, source: Source, raw_rows: list, is_csv: bool = False
) -> tuple[dict, list, str]:
    normalized, results, pending, identities = [], [], {}, {}
    # Fetch approved event identities once; do not perform one observation query per row.
    records = []
    for number, raw in enumerate(raw_rows, 1):
        try:
            records.append((number, row_model(raw, is_csv), None))
        except (ValueError, ValidationError, TypeError, OverflowError, RecursionError) as exc:
            message = (
                "; ".join(
                    f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in exc.errors()
                )
                if isinstance(exc, ValidationError)
                else str(exc)
            )
            records.append((number, None, message[:1000]))
    events = {row.external_event_id for _, row, _ in records if row}
    existing = {
        event_id: hashed
        for event_id, hashed in db.execute(
            select(Observation.external_event_id, Observation.fingerprint).where(
                Observation.source_id == source.id, Observation.external_event_id.in_(events)
            )
        )
    }
    offers = {
        offer.id: offer
        for offer in db.scalars(
            select(Offer).where(Offer.id.in_({row.offer_id for _, row, _ in records if row}))
        )
    }
    products = {
        product.id: product
        for product in db.scalars(
            select(Product).where(Product.id.in_({offer.product_id for offer in offers.values()}))
        )
    }
    mappings = {
        mapping.offer_id: mapping
        for mapping in db.scalars(
            select(SourceOfferMapping).where(
                SourceOfferMapping.source_id == source.id, SourceOfferMapping.offer_id.in_(offers)
            )
        )
    }
    aliases = {row.retailer_id + ":" + row.retailer_listing_id for _, row, _ in records if row}
    alias_targets = {
        (mapping.retailer_id + ":" + mapping.retailer_listing_id): mapping.offer_id
        for mapping in db.scalars(
            select(SourceOfferMapping).where(
                SourceOfferMapping.source_id == source.id,
                SourceOfferMapping.retailer_id.in_(
                    {row.retailer_id for _, row, _ in records if row}
                ),
                SourceOfferMapping.retailer_listing_id.in_(
                    {row.retailer_listing_id for _, row, _ in records if row}
                ),
            )
        )
        if mapping.retailer_id + ":" + mapping.retailer_listing_id in aliases
    }
    for number, row, error in records:
        if row and not error:
            try:
                hashed = fingerprint(row)
                previous = existing.get(row.external_event_id) or pending.get(row.external_event_id)
                if previous and previous != hashed:
                    raise ValueError("Event ID already identifies different price or evidence")
                offer = offers.get(row.offer_id)
                product = products.get(offer.product_id) if offer else None
                if not offer or not product or product.is_demo:
                    raise ValueError("Choose an existing real listing before importing history")
                if not previous:
                    if (
                        row.retailer_id != offer.retailer_id
                        or row.retailer_listing_id != offer.external_id
                    ):
                        raise ValueError("Retailer or listing ID does not match the mapped offer")
                    if not exact_attributes(row.variant_attributes, product.attributes):
                        raise ValueError("Exact variant attributes do not match the mapped product")
                    if context(row) != {
                        "seller": offer.seller,
                        "condition": offer.condition,
                        "region": offer.region,
                    }:
                        raise ValueError(
                            "Seller, condition or region differs from the tracked offer context"
                        )
                    mapping = mappings.get(offer.id)
                    if mapping and (
                        not exact_attributes(mapping.variant_snapshot, row.variant_attributes)
                        or mapping.context_snapshot != context(row)
                    ):
                        raise ValueError(
                            "The existing source mapping requires review before its identity can change"
                        )
                    alias = row.retailer_id + ":" + row.retailer_listing_id
                    if alias in alias_targets and alias_targets[alias] != offer.id:
                        raise ValueError(
                            "This source listing is already mapped to a different offer"
                        )
                    alias_targets[alias] = offer.id
                pending[row.external_event_id] = hashed
                normalized.append(row.model_dump(mode="json"))
                identities[offer.id] = {
                    "retailer_id": offer.retailer_id,
                    "external_id": offer.external_id,
                    "attributes": product.attributes,
                    "seller": offer.seller,
                    "condition": offer.condition,
                    "region": offer.region,
                }
                results.append(
                    {
                        "row": number,
                        "status": "duplicate" if previous else "ready",
                        "offer_id": offer.id,
                        "product_id": product.id,
                        "title": product.title,
                        "retailer_id": offer.retailer_id,
                        "observed_at": utc(row.observed_at).isoformat(),
                        "item_paise": row.item_paise,
                        "message": "Identical evidence already present"
                        if previous
                        else "Exact listing and variant verified",
                    }
                )
                continue
            except ValueError as exc:
                error = str(exc)
        results.append({"row": number, "status": "error", "message": error or "Invalid record"})
    source_state = {
        "id": source.id,
        "display": source.display_allowed,
        "history": source.history_allowed,
        "expires_at": utc(source.expires_at).isoformat() if source.expires_at else None,
        "approval_reference": source.approval_reference,
    }
    digest = hashlib.sha256(
        json.dumps({"source": source_state, "offers": identities}, sort_keys=True).encode()
    ).hexdigest()
    summary = {
        "rows": results,
        "ready": sum(row["status"] == "ready" for row in results),
        "duplicates": sum(row["status"] == "duplicate" for row in results),
        "errors": sum(row["status"] == "error" for row in results),
    }
    return summary, normalized, digest


@router.get("/sources")
def sources(db: Session = Depends(get_db)):
    return [
        {
            "id": source.id,
            "name": source.name,
            "kind": source.kind,
            "approval_reference": source.approval_reference,
            "expires_at": utc(source.expires_at) if source.expires_at else None,
        }
        for source in db.scalars(
            select(Source)
            .where(
                Source.display_allowed.is_(True),
                Source.history_allowed.is_(True),
                Source.kind.not_in(["demo", "public_page"]),
                or_(Source.expires_at.is_(None), Source.expires_at > now()),
            )
            .order_by(Source.name)
        )
    ]


@router.post("/sources", status_code=201)
def approve_source(body: SourceApproval, db: Session = Depends(get_db)):
    if body.expires_at and body.expires_at <= now():
        raise HTTPException(422, "Approval expiry must be in the future")
    source = Source(
        id=body.id,
        name=body.name,
        kind="historical_import",
        display_allowed=True,
        history_allowed=True,
        approval_reference=body.approval_reference,
        expires_at=body.expires_at,
    )
    db.add(source)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            409, "This source ID already exists; its approval was not changed"
        ) from exc
    return {"id": source.id, "name": source.name}


@router.get("/offers")
def offers(q: str = Query(default="", max_length=128), db: Session = Depends(get_db)):
    query = select(Offer, Product).join(Product).where(Product.is_demo.is_(False))
    if q.strip():
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(
            Product.title.ilike("%" + escaped + "%", escape="\\")
            | Offer.id.ilike("%" + escaped + "%", escape="\\")
        )
    return [
        {
            "offer_id": offer.id,
            "product_id": product.id,
            "title": product.title,
            "retailer_id": offer.retailer_id,
            "retailer_listing_id": offer.external_id,
            "variant_attributes": product.attributes,
            "seller": offer.seller,
            "condition": offer.condition,
            "region": offer.region,
        }
        for offer, product in db.execute(query.order_by(Offer.id).limit(50))
    ]


@router.get("/imports")
def recent_imports(db: Session = Depends(get_db)):
    return [
        {
            "id": draft.id,
            "source_name": source.name,
            "file_name": draft.file_name,
            "status": "expired"
            if draft.status == "ready" and utc(draft.expires_at) <= now()
            else draft.status,
            "created_at": utc(draft.created_at),
            "committed_at": utc(draft.committed_at) if draft.committed_at else None,
            "content_sha256": draft.content_sha256,
            "result": draft.result,
        }
        for draft, source in db.execute(
            select(HistoryImport, Source)
            .join(Source)
            .order_by(HistoryImport.created_at.desc())
            .limit(20)
        )
    ]


@router.post("/imports/preview")
async def preview(request: Request, db: Session = Depends(get_db)):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BODY:
            raise HTTPException(413, "Import request is too large; the file limit is 1 MB")
    try:
        file = ImportFile.model_validate(strict_json(data.decode("utf-8")))
    except (ValueError, ValidationError, RecursionError) as exc:
        raise HTTPException(
            422, "Invalid import request; provide a source, file name, format and UTF-8 content"
        ) from exc
    source = permitted_source(db, file.source_id)
    raw_rows, is_csv = parse_file(file)
    summary, payload, digest = inspect_rows(db, source, raw_rows, is_csv)
    draft = None
    if not summary["errors"]:
        draft = HistoryImport(
            id=str(uuid4()),
            source_id=source.id,
            file_name=file.file_name,
            content_sha256=hashlib.sha256(file.content.encode("utf-8")).hexdigest(),
            mapping_digest=digest,
            payload=payload,
            expires_at=now() + timedelta(hours=24),
        )
        db.add(draft)
        db.commit()
    return {
        **summary,
        "import_id": draft.id if draft else None,
        "source_name": source.name,
        "expires_at": draft.expires_at if draft else None,
        "can_commit": not summary["errors"],
    }


@router.post("/imports/{import_id}/commit")
def commit_import(import_id: str, db: Session = Depends(get_db)):
    draft = db.get(HistoryImport, import_id)
    if not draft:
        raise HTTPException(404, "Import preview not found")
    source = permitted_source(db, draft.source_id)
    if draft.status == "committed":
        return {**draft.result, "replayed": True}
    if utc(draft.expires_at) <= now():
        raise HTTPException(409, "Preview expired; upload the file again")
    try:
        claimed = db.execute(
            update(HistoryImport)
            .where(
                HistoryImport.id == draft.id,
                HistoryImport.status == "ready",
                HistoryImport.expires_at > now(),
            )
            .values(status="committing")
            .execution_options(synchronize_session="fetch")
        ).rowcount
        if not claimed:
            db.rollback()
            current = db.get(HistoryImport, import_id)
            if current and current.status == "committed":
                return {**current.result, "replayed": True}
            raise HTTPException(409, "This preview is being committed; retry later")
        # Lock identity rows in a consistent order. SQLite's conditional draft write
        # already serializes writers; PostgreSQL needs explicit identity/source locks.
        if db.bind.dialect.name == "postgresql":
            db.scalar(select(Source).where(Source.id == source.id).with_for_update())
            offer_ids = {record["offer_id"] for record in draft.payload}
            product_ids = list(db.scalars(select(Offer.product_id).where(Offer.id.in_(offer_ids))))
            list(
                db.scalars(
                    select(Product)
                    .where(Product.id.in_(product_ids))
                    .order_by(Product.id)
                    .with_for_update()
                )
            )
            list(
                db.scalars(
                    select(Offer)
                    .where(Offer.id.in_(offer_ids))
                    .order_by(Offer.id)
                    .with_for_update()
                )
            )
        db.expire_all()
        source = permitted_source(db, draft.source_id)
        summary, payload, digest = inspect_rows(db, source, draft.payload)
        if summary["errors"] or digest != draft.mapping_digest:
            db.rollback()
            raise HTTPException(
                409,
                "Source, listing identity or evidence changed after preview; create a new preview",
            )
        inserted, duplicates, products, seen = 0, 0, set(), set()
        existing = set(
            db.scalars(
                select(Observation.external_event_id).where(
                    Observation.source_id == source.id,
                    Observation.external_event_id.in_(
                        [record["external_event_id"] for record in payload]
                    ),
                )
            )
        )
        offer_ids = {record["offer_id"] for record in payload}
        imported_offers = {
            offer.id: offer for offer in db.scalars(select(Offer).where(Offer.id.in_(offer_ids)))
        }
        mapped = set(
            db.scalars(
                select(SourceOfferMapping.offer_id).where(
                    SourceOfferMapping.source_id == source.id,
                    SourceOfferMapping.offer_id.in_(offer_ids),
                )
            )
        )
        for raw in payload:
            row = ImportRow.model_validate(raw)
            offer = imported_offers[row.offer_id]
            products.add(offer.product_id)
            if row.external_event_id in existing or row.external_event_id in seen:
                duplicates += 1
                continue
            seen.add(row.external_event_id)
            if offer.id not in mapped:
                db.add(
                    SourceOfferMapping(
                        source_id=source.id,
                        offer_id=offer.id,
                        retailer_id=offer.retailer_id,
                        retailer_listing_id=offer.external_id,
                        variant_snapshot=row.variant_attributes,
                        context_snapshot=context(row),
                    )
                )
                mapped.add(offer.id)
            db.add(
                Observation(
                    offer_id=offer.id,
                    source_id=source.id,
                    external_event_id=row.external_event_id,
                    fingerprint=fingerprint(row),
                    observed_at=utc(row.observed_at),
                    item_paise=row.item_paise,
                    shipping_paise=row.shipping_paise,
                    mrp_paise=row.mrp_paise,
                    in_stock=row.in_stock,
                    currency=row.currency,
                    evidence_kind="historical_import",
                    import_id=draft.id,
                    evidence_reference=row.evidence_reference,
                    price_type=row.price_type,
                    variant_snapshot=row.variant_attributes,
                    context_snapshot={
                        **context(row),
                        "availability": "in_stock" if row.in_stock else "out_of_stock",
                    },
                )
            )
            inserted += 1
        draft.status = "committed"
        draft.committed_at = now()
        draft.result = {
            "import_id": draft.id,
            "inserted": inserted,
            "duplicates": duplicates,
            "product_ids": sorted(products),
            "replayed": False,
        }
        db.commit()
        return draft.result
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            409, "Concurrent event or mapping conflict; preview again. No partial import was saved"
        ) from exc
