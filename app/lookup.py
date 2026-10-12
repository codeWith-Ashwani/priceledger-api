import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.collection import public_source, record_attempt, reserve_requests, touch_offer, track_offer
from app.config import get_settings
from app.db import get_db, now, utc
from app.discovery import learn_store, registered_stores
from app.identity import model_attributes
from app.ingestion import ingest
from app.limits import lookup_limit
from app.models import Offer, Product, Retailer, Source
from app.retail import (
    SHOPIFY_ORIGINS,
    STORES,
    Listing,
    LookupError,
    Target,
    discovery_retailers,
    load_listing,
    search_store,
    store_info,
    target_for,
)
from app.schemas import IngestBatch, ObservationInput

router = APIRouter(prefix="/api/v1")


class LookupInput(BaseModel):
    query: str = Field(min_length=3, max_length=2048)


def store_listing(
    db: Session,
    listing: Listing,
    *,
    commit: bool = True,
    manage_job: bool = True,
    started_at=None,
) -> str:
    target = listing.target
    product_id = f"{target.retailer}:{target.external_id}"
    offer_id = product_id + ":public-page"
    source_id = "public-page:" + target.retailer
    store_name, store_color = store_info(target)
    public_source(db, target)
    if db.get(Retailer, target.retailer) is None:
        db.add(
            Retailer(
                id=target.retailer,
                name=store_name,
                color=store_color,
            )
        )
    product = db.get(Product, product_id)
    previous_attributes = product.attributes if product else {}
    if previous_attributes.get("identity_category") == "mobile" and re.search(
        r"\b(?:headphones?|earbuds?|headset)\b", product.title, re.I
    ):
        previous_attributes = model_attributes(product.title, product.brand, previous_attributes)
    if product is None:
        product = Product(
            id=product_id,
            title=listing.title,
            brand=listing.brand,
            category=listing.category,
            variant_label=listing.title[:200],
            attributes=listing.attributes,
            illustration="product",
            image_url=listing.image_url,
            is_demo=False,
        )
        db.add(product)
    elif any(
        value is not None and value != "" and listing.attributes.get(key) != value
        for key, value in previous_attributes.items()
    ):
        # A changed retailer variant must not silently overwrite the tracked identity.
        raise LookupError(
            "variant_changed",
            "The listing's variant metadata has changed. This observation needs review before it can join the history.",
            409,
        )
    else:
        product.attributes = listing.attributes
        product.title = listing.title
        product.image_url = listing.image_url
    db.flush()
    offer = db.get(Offer, offer_id)
    if offer is None:
        offer = Offer(
            id=offer_id,
            product_id=product_id,
            retailer_id=target.retailer,
            source_id=source_id,
            external_id=target.external_id,
            context_key="IN:public-page",
            seller=listing.seller,
            condition=listing.condition,
            product_url=target.url,
            channel="online",
            availability=listing.availability,
        )
        db.add(offer)
        db.flush()
    if (
        offer.condition != "unknown"
        and listing.condition != "unknown"
        and offer.condition != listing.condition
    ):
        raise LookupError(
            "variant_changed",
            "The listing condition changed; its prices cannot join the previous variant history.",
            409,
        )
    offer.availability = listing.availability
    offer.product_url = target.url
    offer.seller = listing.seller
    offer.condition = listing.condition
    observed_at = now()
    offer.rating = (
        {**listing.rating, "observed_at": observed_at.isoformat()} if listing.rating else None
    )
    ingest(
        db,
        IngestBatch(
            source_id=source_id,
            observations=[
                ObservationInput(
                    offer_id=offer_id,
                    external_event_id="page-check:" + uuid4().hex,
                    observed_at=observed_at,
                    variant_attributes=product.attributes,
                    item_paise=listing.item_paise,
                    shipping_paise=listing.shipping_paise,
                    in_stock=listing.availability == "in_stock",
                    evidence_kind="page_check",
                    listed_fees=listing.listed_fees,
                )
            ],
        ),
        demo_mode=False,
        commit=False,
    )
    if manage_job:
        track_offer(db, offer, requested=True)
        record_attempt(db, target, started_at=started_at or observed_at)
    learn_store(db, listing)
    if commit:
        db.commit()
    else:
        db.flush()
    return product_id


def lookup_link(db: Session, query: str) -> dict:
    target = target_for(query)
    cached = db.scalar(select(Offer).where(Offer.product_url == target.url))
    at = now()
    if (
        cached
        and cached.current_observed_at
        and at - timedelta(minutes=get_settings().lookup_cache_minutes)
        < utc(cached.current_observed_at)
        <= at
    ):
        source = db.get(Source, cached.source_id)
        product = db.get(Product, cached.product_id)
        if (
            source
            and source.display_allowed
            and source.history_allowed
            and source.kind != "demo"
            and (source.expires_at is None or utc(source.expires_at) > at)
            and product
            and not product.is_demo
        ):
            touch_offer(db, cached)
            return {
                "kind": "product",
                "product_id": cached.product_id,
                "cached": True,
                "message": "Showing the latest recorded retailer observation.",
            }
    reserve_requests(db, target)
    started_at = now()
    try:
        listing = load_listing(target)
        try:
            product_id = store_listing(db, listing, started_at=started_at)
        except IntegrityError:
            db.rollback()
            product_id = store_listing(db, listing, started_at=started_at)
    except LookupError as exc:
        db.rollback()
        record_attempt(db, target, started_at=started_at, error_code=exc.code)
        db.commit()
        raise
    return {
        "kind": "product",
        "product_id": product_id,
        "cached": False,
        "message": "A real retailer-page price was recorded. Earlier history is only shown if recorded evidence exists."
        if listing.item_paise is not None
        else "Product identified, but this store did not expose a current price. Checking other stores for this model.",
    }


@router.post("/lookup", dependencies=[Depends(lookup_limit)])
def lookup(body: LookupInput, db: Session = Depends(get_db)):
    query = body.query.strip()
    try:
        if len(query) < 3:
            raise LookupError(
                "invalid_search",
                "Enter a product link or at least three characters of its brand and model.",
                400,
            )
        if query.lower().startswith(("https://", "http://")):
            return lookup_link(db, query)
        if len(query) > 128 or "://" in query or ".com/" in query or ".in/" in query:
            raise LookupError(
                "invalid_search",
                "Paste a full product URL or enter a brand, model and variant (up to 128 characters).",
                400,
            )
        brand = next(
            (
                brand
                for _, brand in SHOPIFY_ORIGINS.values()
                if re.match(
                    (r"fire[- ]?boltt" if brand == "Fire-Boltt" else re.escape(brand)) + r"\s+",
                    query,
                    re.I,
                )
            ),
            None,
        )
        custom = registered_stores(db, brand)
        permitted, limited = reserve_stores(
            db, [*discovery_retailers(), *custom], cost=1, custom=custom
        )
        with ThreadPoolExecutor(max_workers=get_settings().discovery_workers) as pool:
            stores = list(
                pool.map(
                    lambda retailer: (
                        search_store(retailer, query, custom=custom[retailer])
                        if retailer in custom
                        else search_store(retailer, query)
                    ),
                    permitted,
                )
            )
        for store in stores:
            if store["status"] not in {"ok", "no_matches"}:
                record_attempt(
                    db,
                    Target(store["retailer"], store["search_url"], None),
                    started_at=now(),
                    error_code=store["status"],
                )
        db.commit()
        stores.extend(limited.values())
        return {
            "kind": "search",
            "query": query,
            "stores": stores,
            "message": "Choose an exact listing to check its current price and recorded history.",
        }
    except LookupError as exc:
        db.rollback()
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from exc


def reserve_stores(
    db: Session, retailers: list[str], *, cost: int, custom: dict | None = None
) -> tuple[list[str], dict]:
    permitted, limited = [], {}
    for retailer in retailers:
        spec = (custom or {}).get(retailer)
        name, url = (
            (spec["name"], spec["search_base"])
            if spec
            else (STORES[retailer][0], STORES[retailer][2])
        )
        try:
            # Official-store search reads its catalog plus one model's SKU list.
            reserve_requests(db, Target(retailer, url, None), cost + (retailer == "iqoo"))
            permitted.append(retailer)
        except LookupError as exc:
            db.rollback()
            limited[retailer] = {
                "retailer": retailer,
                "retailer_name": name,
                "status": exc.code,
                "message": exc.message,
                "search_url": url,
                "items": [],
            }
    return permitted, limited
