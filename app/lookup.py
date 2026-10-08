from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Lock
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, now, utc
from app.ingestion import ingest
from app.models import Offer, Product, Retailer, Source
from app.retail import (
    STORES,
    Listing,
    LookupError,
    load_listing,
    search_store,
    store_info,
    target_for,
)
from app.schemas import IngestBatch, ObservationInput

router = APIRouter(prefix="/api/v1")
_requests: dict[str, deque] = defaultdict(deque)
_lock = Lock()


class LookupInput(BaseModel):
    query: str = Field(min_length=3, max_length=2048)


def lookup_limit(request: Request):
    client = request.client.host if request.client else "local"
    action = "comparison" if request.url.path.endswith("/compare") else "lookup"
    key = f"{client}:{action}"
    at = monotonic()
    with _lock:
        # Bounded process-local limiter. Use a shared limiter before deploying replicas.
        for old_key in list(_requests):
            if not _requests[old_key] or _requests[old_key][-1] <= at - 60:
                del _requests[old_key]
        queue = _requests[key]
        while queue and queue[0] <= at - 60:
            queue.popleft()
        if len(queue) >= get_settings().lookup_requests_per_minute:
            raise HTTPException(
                429,
                "Too many lookups. Wait one minute and try again.",
                headers={"Retry-After": "60"},
            )
        if len(_requests) > 10_000:
            raise HTTPException(503, "Lookup capacity is temporarily full.")
        queue.append(at)


def store_listing(db: Session, listing: Listing) -> str:
    target = listing.target
    product_id = f"{target.retailer}:{target.external_id}"
    offer_id = product_id + ":public-page"
    source_id = "public-page:" + target.retailer
    store_name, store_color = store_info(target)
    source = db.get(Source, source_id)
    if source is None:
        source = Source(
            id=source_id,
            name=store_name + " product page",
            kind="public_page",
            display_allowed=True,
            history_allowed=True,
        )
        db.add(source)
    if (
        not source.display_allowed
        or not source.history_allowed
        or (source.expires_at and utc(source.expires_at) <= now())
    ):
        raise LookupError(
            "source_disabled", "This source has been disabled. No price was recorded.", 403
        )
    if db.get(Retailer, target.retailer) is None:
        db.add(
            Retailer(
                id=target.retailer,
                name=store_name,
                color=store_color,
            )
        )
    product = db.get(Product, product_id)
    if product is None:
        product = Product(
            id=product_id,
            title=listing.title,
            brand=listing.brand,
            category=listing.category,
            variant_label=listing.title,
            attributes=listing.attributes,
            illustration="product",
            image_url=listing.image_url,
            is_demo=False,
        )
        db.add(product)
    elif any(listing.attributes.get(key) != value for key, value in product.attributes.items()):
        # A changed retailer variant must not silently overwrite the tracked identity.
        raise LookupError(
            "variant_changed",
            "The listing's variant metadata has changed. This observation needs review before it can join the history.",
            409,
        )
    else:
        product.attributes = listing.attributes
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
    ingest(
        db,
        IngestBatch(
            source_id=source_id,
            observations=[
                ObservationInput(
                    offer_id=offer_id,
                    external_event_id=f"{target.external_id}:{observed_at.isoformat()}",
                    observed_at=observed_at,
                    variant_attributes=product.attributes,
                    item_paise=listing.item_paise,
                    shipping_paise=listing.shipping_paise,
                    in_stock=listing.availability == "in_stock",
                )
            ],
        ),
        demo_mode=False,
        commit=False,
    )
    db.commit()
    return product_id


def lookup_link(db: Session, query: str) -> dict:
    target = target_for(query)
    cached = db.scalar(select(Offer).where(Offer.product_url == target.url))
    if (
        cached
        and cached.current_observed_at
        and utc(cached.current_observed_at)
        > now() - timedelta(minutes=get_settings().lookup_cache_minutes)
    ):
        source = db.get(Source, cached.source_id)
        product = db.get(Product, cached.product_id)
        if (
            source
            and source.display_allowed
            and source.history_allowed
            and (source.expires_at is None or utc(source.expires_at) > now())
            and product
            and not product.is_demo
        ):
            return {
                "kind": "product",
                "product_id": cached.product_id,
                "cached": True,
                "message": "Showing the latest recorded retailer observation.",
            }
    listing = load_listing(target)
    try:
        product_id = store_listing(db, listing)
    except IntegrityError:
        db.rollback()
        # Another request may have created this same listing; retry against that identity.
        product_id = store_listing(db, listing)
    return {
        "kind": "product",
        "product_id": product_id,
        "cached": False,
        "message": "A real retailer-page price was recorded. Earlier history is only shown if recorded evidence exists.",
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
        with ThreadPoolExecutor(max_workers=3) as pool:
            stores = list(pool.map(lambda retailer: search_store(retailer, query), STORES))
        return {
            "kind": "search",
            "query": query,
            "stores": stores,
            "message": "Choose an exact listing to check its current price and recorded history.",
        }
    except LookupError as exc:
        db.rollback()
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from exc
