from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, now, utc
from app.models import Observation, Offer, Product, Retailer, Source

router = APIRouter(prefix="/api/v1")


def approved_sources():
    conditions = [
        Source.history_allowed.is_(True),
        Source.display_allowed.is_(True),
        (Source.expires_at.is_(None) | (Source.expires_at > now())),
    ]
    if not get_settings().demo_mode:
        conditions.append(Source.kind != "demo")
    return conditions


def product_json(product: Product, offers: list[dict]) -> dict:
    comparable = [
        offer["total_paise"]
        for offer in offers
        if offer["in_stock"] and not offer["stale"] and offer["total_paise"] is not None
    ]
    return {
        "id": product.id,
        "title": product.title,
        "brand": product.brand,
        "category": product.category,
        "variant_label": product.variant_label,
        "attributes": product.attributes,
        "illustration": product.illustration,
        "is_demo": product.is_demo,
        "best_price_paise": min(comparable, default=None),
        "retailer_count": len(offers),
    }


def offers_for(db: Session, product_ids: list[str]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {product_id: [] for product_id in product_ids}
    if not product_ids:
        return grouped
    rows = db.execute(
        select(Offer, Retailer, Source)
        .join(Retailer)
        .join(Source)
        .where(Offer.product_id.in_(product_ids), *approved_sources())
    )
    for offer, retailer, source in rows:
        timestamp = utc(offer.current_observed_at) if offer.current_observed_at else None
        stale = timestamp is None or timestamp < now() - timedelta(
            hours=get_settings().freshness_hours
        )
        item, shipping = offer.current_item_paise, offer.current_shipping_paise
        total = item + shipping if item is not None and shipping is not None else None
        grouped[offer.product_id].append(
            {
                "id": offer.id,
                "retailer_id": retailer.id,
                "retailer_name": retailer.name,
                "color": retailer.color,
                "seller": offer.seller,
                "condition": offer.condition,
                "region": offer.region,
                "item_paise": item,
                "shipping_paise": shipping,
                "total_paise": total,
                "in_stock": offer.current_in_stock,
                "stale": stale,
                "observed_at": timestamp,
                "source_name": source.name,
                "is_demo": source.kind == "demo",
            }
        )
    return grouped


@router.get("/categories")
def categories(db: Session = Depends(get_db)):
    query = select(Product.category, func.count(Product.id)).group_by(Product.category)
    if not get_settings().demo_mode:
        query = query.where(Product.is_demo.is_(False))
    return [
        {"id": category, "name": category.replace("-", " ").title(), "count": count}
        for category, count in db.execute(query.order_by(Product.category))
    ]


@router.get("/products")
def products(
    q: str = Query(default="", max_length=128),
    category: str | None = None,
    limit: int = Query(default=24, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    conditions = []
    if not get_settings().demo_mode:
        conditions.append(Product.is_demo.is_(False))
    if q.strip():
        pattern = (
            "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        conditions.append(
            (Product.title.ilike(pattern, escape="\\") | Product.brand.ilike(pattern, escape="\\"))
        )
    if category:
        conditions.append(Product.category == category)
    total = db.scalar(select(func.count()).select_from(Product).where(*conditions))
    records = list(
        db.scalars(
            select(Product).where(*conditions).order_by(Product.id).offset(offset).limit(limit)
        )
    )
    grouped = offers_for(db, [record.id for record in records])
    return {
        "items": [product_json(record, grouped[record.id]) for record in records],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_product(db: Session, product_id: str) -> Product:
    product = db.get(Product, product_id)
    if not product or (product.is_demo and not get_settings().demo_mode):
        raise HTTPException(404, "Product not found")
    return product


@router.get("/products/{product_id}")
def detail(product_id: str, db: Session = Depends(get_db)):
    product = get_product(db, product_id)
    offers = offers_for(db, [product_id])[product_id]
    first_seen = db.scalar(
        select(func.min(Observation.observed_at))
        .select_from(Observation)
        .join(Offer, Observation.offer_id == Offer.id)
        .join(Source, Observation.source_id == Source.id)
        .where(Offer.product_id == product_id, *approved_sources())
    )
    return {
        **product_json(product, offers),
        "offers": offers,
        "first_observed_at": utc(first_seen) if first_seen else None,
        "launch_date": None,
        "launch_date_verified": False,
    }


@router.get("/products/{product_id}/history")
def history(
    product_id: str, days: int = Query(default=90, ge=1, le=3650), db: Session = Depends(get_db)
):
    get_product(db, product_id)
    start = now() - timedelta(days=days)
    rows = db.execute(
        select(Observation, Offer, Retailer)
        .select_from(Observation)
        .join(Offer, Observation.offer_id == Offer.id)
        .join(Retailer, Offer.retailer_id == Retailer.id)
        .join(Source, Observation.source_id == Source.id)
        .where(
            Offer.product_id == product_id,
            Observation.observed_at >= start,
            Observation.observed_at <= now(),
            *approved_sources(),
        )
        .order_by(Observation.observed_at)
    )
    series = {}
    for observation, offer, retailer in rows:
        if offer.id not in series:
            series[offer.id] = {
                "offer_id": offer.id,
                "retailer_id": retailer.id,
                "name": retailer.name,
                "color": retailer.color,
                "points": [],
            }
        item, shipping = observation.item_paise, observation.shipping_paise
        total = (
            item + shipping
            if observation.in_stock and item is not None and shipping is not None
            else None
        )
        series[offer.id]["points"].append(
            {
                "at": utc(observation.observed_at),
                "total_paise": total,
                "item_paise": item,
                "shipping_paise": shipping,
                "in_stock": observation.in_stock,
            }
        )
    return {
        "product_id": product_id,
        "days": days,
        "currency": "INR",
        "series": list(series.values()),
        "max_gap_hours": get_settings().history_gap_hours,
    }
