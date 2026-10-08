from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog import get_product
from app.collection import record_attempt, reserve_requests
from app.coordination import claim_comparison, fence_comparison, release_comparison
from app.db import get_db, now, utc
from app.identity import match_variant
from app.limits import lookup_limit
from app.lookup import reserve_stores, store_listing
from app.models import ComparisonScan, Offer, Product, ProductMatch, Source
from app.retail import (
    STORES,
    Listing,
    LookupError,
    Target,
    load_listing,
    search_store,
    store_info,
    target_for,
)

router = APIRouter(prefix="/api/v1")


def listing_from_record(product: Product, offer: Offer) -> Listing:
    return Listing(
        target_for(offer.product_url),
        product.title,
        product.brand,
        product.category,
        product.attributes,
        product.image_url,
        offer.current_item_paise or 0,
        offer.current_shipping_paise,
        offer.availability,
        offer.condition,
        offer.seller,
    )


def discover_store(retailer: str, query: str) -> dict:
    result = search_store(retailer, query)
    candidates = []
    failures = []
    failed_checks = []
    # Bounded verification: read at most two exact search candidates from each store.
    for item in result["items"][:2]:
        started_at = now()
        try:
            candidates.append(load_listing(target_for(item["url"])))
        except LookupError as exc:
            failures.append(exc.message)
            failed_checks.append({"url": item["url"], "code": exc.code, "at": started_at})
            if exc.code == "retailer_blocked":
                break
    return {
        "search": result,
        "listings": candidates,
        "failures": failures,
        "failed_checks": failed_checks,
    }


def scan_comparison(db: Session, product_id: str, *, lease_token: str | None = None) -> dict:
    product = get_product(db, product_id)
    existing = db.get(ComparisonScan, product_id)
    if existing and utc(existing.checked_at) > now() - timedelta(minutes=15):
        # Revoked sources must not leak cached prices or verified links.
        source_ids = existing.result.get("source_ids", [])
        sources = list(db.scalars(select(Source).where(Source.id.in_(source_ids))))
        if len(sources) == len(source_ids) and all(
            source.display_allowed
            and source.history_allowed
            and (source.expires_at is None or utc(source.expires_at) > now())
            for source in sources
        ):
            return {**existing.result, "cached": True}
    offer = db.scalar(
        select(Offer)
        .where(Offer.product_id == product_id, Offer.product_url.is_not(None))
        .order_by(Offer.current_observed_at.desc())
    )
    if not offer:
        raise HTTPException(
            422, "This product needs a retailer listing before other stores can be compared."
        )
    source = db.get(Source, offer.source_id)
    if (
        not source
        or not source.display_allowed
        or not source.history_allowed
        or (source.expires_at and utc(source.expires_at) <= now())
    ):
        raise HTTPException(403, "This product source has been disabled.")
    anchor = listing_from_record(product, offer)
    # Enrich old records with newly available variant metadata from the same listing.
    if "identity_category" not in anchor.attributes and "phone" in anchor.title.lower():
        try:
            reserve_requests(db, anchor.target)
            anchor = load_listing(anchor.target)
            store_listing(db, anchor)
        except LookupError:
            db.rollback()
    model = anchor.attributes.get("model", "")
    query = f"{anchor.brand} {model}" if model else anchor.title
    for key in ("storage_gb", "color"):
        if key in anchor.attributes:
            query += " " + str(anchor.attributes[key])
    query = query[:128]
    retailers = [retailer for retailer in STORES if retailer != anchor.target.retailer]
    # Reserve one search and up to two page reads per store before parallel discovery.
    permitted, limited = reserve_stores(db, retailers, cost=3)
    with ThreadPoolExecutor(max_workers=3) as pool:
        discoveries = list(pool.map(lambda retailer: discover_store(retailer, query), permitted))
    by_store = dict(zip(permitted, discoveries, strict=True))
    discovered = [
        by_store.get(retailer)
        or {"search": limited[retailer], "listings": [], "failures": [limited[retailer]["message"]]}
        for retailer in retailers
    ]
    stores = []
    source_ids = {offer.source_id}
    for retailer, discovery in zip(retailers, discovered, strict=True):
        search = discovery["search"]
        if retailer in permitted and search["status"] not in {"ok", "no_matches"}:
            record_attempt(
                db,
                Target(retailer, search["search_url"], None),
                started_at=now(),
                error_code=search["status"],
            )
        for failed in discovery.get("failed_checks", []):
            record_attempt(
                db, target_for(failed["url"]), started_at=failed["at"], error_code=failed["code"]
            )
        db.commit()
        items = []
        for candidate in discovery["listings"]:
            decision = match_variant(anchor, candidate)
            try:
                candidate_id = store_listing(db, candidate)
                source_ids.add("public-page:" + candidate.target.retailer)
                if decision["status"] == "verified":
                    left, right = sorted((product_id, candidate_id))
                    if left != right and db.get(ProductMatch, (left, right)) is None:
                        db.add(
                            ProductMatch(
                                left_id=left,
                                right_id=right,
                                method=decision["method"],
                                evidence={"reason": decision["reason"]},
                            )
                        )
                        db.commit()
                items.append(
                    {
                        "product_id": candidate_id,
                        "title": candidate.title,
                        "url": candidate.target.url,
                        "item_paise": candidate.item_paise,
                        "shipping_paise": candidate.shipping_paise,
                        "availability": candidate.availability,
                        "condition": candidate.condition,
                        "match_status": decision["status"],
                        "reason": decision["reason"],
                        "observed_at": now().isoformat(),
                        "source_name": store_info(candidate.target)[0] + " product page",
                    }
                )
            except (LookupError, IntegrityError) as exc:
                db.rollback()
                discovery["failures"].append(
                    str(exc)
                    if isinstance(exc, LookupError)
                    else "This listing is being updated; retry later."
                )
        verified = sum(item["match_status"] == "verified" for item in items)
        stores.append(
            {
                "retailer": retailer,
                "retailer_name": STORES[retailer][0],
                "status": "verified"
                if verified
                else "review"
                if any(item["match_status"] == "review" for item in items)
                else "different_variant"
                if items
                else search["status"]
                if search["status"] != "ok"
                else "no_verified_match",
                "message": f"{verified} matching offer(s) joined the comparison."
                if verified
                else "Possible matches have separate histories until their variant is verified."
                if any(item["match_status"] == "review" for item in items)
                else "These readable listings are different variants and are excluded from the comparison."
                if items
                else (
                    discovery["failures"][0]
                    if discovery["failures"]
                    else "No verified offer for this exact variant was readable."
                ),
                "search_url": search["search_url"],
                "items": items,
            }
        )
    result = {
        "product_id": product_id,
        "stores": stores,
        "checked_at": now().isoformat(),
        "cached": False,
        "history_note": "Historical prices come from actual recorded or imported observations. A current page does not expose every past price.",
        "source_ids": sorted(source_ids),
    }
    if lease_token:
        fence_comparison(db, product_id, lease_token)
    scan = db.get(ComparisonScan, product_id)
    if scan:
        scan.result, scan.checked_at = result, now()
    else:
        db.add(ComparisonScan(product_id=product_id, result=result, checked_at=now()))
    db.commit()
    return result


@router.post("/products/{product_id}/compare", dependencies=[Depends(lookup_limit)])
def compare(product_id: str, db: Session = Depends(get_db)):
    get_product(db, product_id)
    token = claim_comparison(db, product_id)
    try:
        return scan_comparison(db, product_id, lease_token=token)
    finally:
        release_comparison(db, product_id, token)
