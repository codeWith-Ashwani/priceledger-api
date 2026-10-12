import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog import get_product, offers_for
from app.collection import record_attempt, reserve_requests
from app.config import get_settings
from app.coordination import claim_comparison, fence_comparison, release_comparison
from app.db import get_db, now, utc
from app.discovery import registered_stores
from app.evidence import approved_sources
from app.identity import bundle_signature, match_model, match_variant, model_attributes, model_key
from app.limits import lookup_limit
from app.lookup import reserve_stores, store_listing
from app.models import ComparisonScan, Offer, Product, ProductMatch, Source
from app.retail import (
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

router = APIRouter(prefix="/api/v1")
DISCOVERY_VERSION = 15


def link_catalog_matches(db: Session, product_id: str, anchor: Listing):
    # Any previously tracked public retailer can participate, even without a
    # search adapter. Stream candidates and still require exact identity.
    rows = db.execute(
        select(Product, Offer)
        .select_from(Offer)
        .join(Product, Offer.product_id == Product.id)
        .join(Source, Offer.source_id == Source.id)
        .where(
            Product.id != product_id,
            Product.is_demo.is_(False),
            func.lower(Product.brand) == anchor.brand.lower(),
            Offer.retailer_id != anchor.target.retailer,
            Offer.product_url.is_not(None),
            *approved_sources(),
        )
        .order_by(Offer.id)
        .execution_options(yield_per=100)
    )
    for product, offer in rows:
        try:
            candidate = listing_from_record(product, offer)
        except LookupError:
            continue
        decision = match_variant(anchor, candidate)
        if decision["status"] != "verified":
            continue
        left, right = sorted((product_id, product.id))
        if db.get(ProductMatch, (left, right)) is None:
            try:
                with db.begin_nested():
                    db.add(
                        ProductMatch(
                            left_id=left,
                            right_id=right,
                            method=decision["method"],
                            evidence={"reason": decision["reason"], "origin": "tracked_catalog"},
                        )
                    )
                    db.flush()
            except IntegrityError:
                # Opposite anchor scans can discover the same pair concurrently.
                if db.get(ProductMatch, (left, right)) is None:
                    raise
    db.commit()


def listing_from_record(product: Product, offer: Offer) -> Listing:
    return Listing(
        target_for(offer.product_url),
        product.title,
        product.brand,
        product.category,
        model_attributes(product.title, product.brand, product.attributes),
        product.image_url,
        offer.current_item_paise,
        offer.current_shipping_paise,
        offer.availability,
        offer.condition,
        offer.seller,
        listed_fees=offer.current_listed_fees,
    )


def candidate_priority(item: dict, expected: dict) -> tuple[int, int, int, int]:
    actual = model_attributes(item["title"], str(expected.get("_brand", "")), {})
    actual.update(
        {key: value for key, value in item.get("variant_attributes", {}).items() if key != "model"}
    )
    conflicts, agreements = 0, 0
    if (
        actual.get("model")
        and expected.get("model")
        and model_key(actual["model"]) != model_key(expected["model"])
    ):
        conflicts += 10
    for key in ("ram_gb", "storage_gb", "color"):
        if key in actual and key in expected:
            if str(actual[key]).lower() == str(expected[key]).lower():
                agreements += 1
            else:
                conflicts += 1
    incompatible = bundle_signature(item["title"]) != bundle_signature(
        str(expected.get("listing_title", ""))
    )
    incompatible = incompatible or bool(
        re.search(r"\b(?:case|cover|protector|replacement|skin|sleeve)\b", item["title"], re.I)
    ) != bool(
        re.search(
            r"\b(?:case|cover|protector|replacement|skin|sleeve)\b",
            str(expected.get("listing_title", "")),
            re.I,
        )
    )
    requested_url = urlsplit(str(expected.get("_url", "")))
    candidate_url = urlsplit(str(item.get("url", "")))
    same_page = bool(
        requested_url.hostname
        and requested_url.hostname == candidate_url.hostname
        and requested_url.path.rstrip("/") == candidate_url.path.rstrip("/")
    )
    return int(incompatible), conflicts, -agreements, int(not same_page)


def discovery_query(anchor: Listing) -> str:
    model = model_attributes(anchor.title, anchor.brand, anchor.attributes).get("model")
    if model:
        # Charging/connectivity labels are optional search terms, never ignored
        # during exact identity verification.
        model = re.sub(r"\b(?:\d{1,3}W|[2345]G)\b", "", str(model), flags=re.I)
        return re.sub(r"\s+", " ", f"{anchor.brand} {model}").strip()[:128]
    return anchor.title.split("|", 1)[0][:128]


def model_offers(db: Session, product_id: str, anchor: Listing) -> list[dict]:
    rows = db.execute(
        select(Product, Offer)
        .join(Offer, Offer.product_id == Product.id)
        .join(Source, Offer.source_id == Source.id)
        .where(
            Product.id != product_id,
            Product.is_demo.is_(False),
            func.lower(Product.brand) == anchor.brand.lower(),
            Offer.product_url.is_not(None),
            *approved_sources(),
        )
        .order_by(Offer.id)
        .execution_options(yield_per=100)
    )
    decisions = {}
    for product, offer in rows:
        try:
            decision = match_model(anchor, listing_from_record(product, offer))
        except LookupError:
            continue
        if decision["status"] == "model_match":
            decisions[product.id] = decision
    # Batch database parameters, not result coverage: retain every matched offer.
    members = list(decisions)
    result = []
    for offset in range(0, len(members), 100):
        grouped = offers_for(db, members[offset : offset + 100])
        result.extend(
            {**offer, "match_scope": "model", "differences": decisions[member]["differences"]}
            for member, offers in grouped.items()
            for offer in offers
        )
    return result


def discover_store(
    retailer: str, query: str, expected: dict | None = None, *, custom: dict | None = None
) -> dict:
    result = (
        search_store(retailer, query, custom=custom) if custom else search_store(retailer, query)
    )
    candidates = []
    failures = []
    failed_checks = []
    seen = set()
    # Verification bounds and source reservations use the same configured limit.
    ranked = sorted(result["items"], key=lambda item: candidate_priority(item, expected or {}))
    for item in ranked:
        if len(seen) >= get_settings().discovery_candidates_per_store:
            break
        started_at = now()
        target = None
        try:
            candidate_target = target_for(item["url"])
            if candidate_target.retailer != retailer:
                raise LookupError(
                    "retailer_mismatch",
                    "The search returned a listing from another retailer; it was excluded.",
                )
            if candidate_target.url in seen:
                continue
            seen.add(candidate_target.url)
            target = candidate_target
            candidates.append(load_listing(target))
        except LookupError as exc:
            failures.append(exc.message)
            if target is not None:
                failed_checks.append({"target": target, "code": exc.code, "at": started_at})
            if exc.code == "retailer_blocked":
                break
    return {
        "search": result,
        "listings": candidates,
        "failures": failures,
        "failed_checks": failed_checks,
    }


def scan_comparison(db: Session, product_id: str, *, lease_token: str | None = None) -> dict:
    settings = get_settings()
    product = get_product(db, product_id)
    at = now()
    anchor_query = select(Offer).where(
        Offer.product_id == product_id, Offer.product_url.is_not(None)
    )
    offer = db.scalar(
        anchor_query.join(Source, Offer.source_id == Source.id)
        .where(*approved_sources(at))
        .order_by(Offer.current_observed_at.desc(), Offer.id)
    )
    if not offer:
        if db.scalar(anchor_query.limit(1)):
            raise HTTPException(403, "This product source has been disabled.")
        raise HTTPException(
            422, "This product needs a retailer listing before other stores can be compared."
        )
    anchor = listing_from_record(product, offer)
    existing = db.get(ComparisonScan, product_id)
    anchor_retailers = set(
        db.scalars(
            select(Offer.retailer_id).where(
                Offer.product_id == product_id, Offer.current_item_paise.is_not(None)
            )
        )
    )
    custom = registered_stores(db, product.brand)
    selected = [
        retailer
        for retailer in [*discovery_retailers(product.category, product.brand), *custom]
        if retailer not in anchor_retailers
    ]
    if (
        existing
        and existing.result.get("discovery_version") == DISCOVERY_VERSION
        and existing.result.get("candidates_per_store") == settings.discovery_candidates_per_store
        and existing.result.get("discovery_retailers") == selected
        and at - timedelta(minutes=settings.comparison_cache_minutes)
        < utc(existing.checked_at)
        <= at
    ):
        # Revoked sources must not leak cached prices or verified links.
        source_ids = existing.result.get("source_ids", [])
        permitted_sources = set(
            db.scalars(select(Source.id).where(Source.id.in_(source_ids), *approved_sources(at)))
        )
        if permitted_sources == set(source_ids):
            link_catalog_matches(db, product_id, anchor)
            return {
                **existing.result,
                "model_offers": model_offers(db, product_id, anchor),
                "cached": True,
            }
    link_catalog_matches(db, product_id, anchor)
    # Enrich old records with newly available variant metadata from the same listing.
    if (
        "identity_category" not in product.attributes
        and anchor.attributes.get("identity_category") == "mobile"
    ):
        try:
            reserve_requests(db, anchor.target)
            anchor = load_listing(anchor.target)
            store_listing(db, anchor)
        except LookupError:
            db.rollback()
    query = discovery_query(anchor)
    retailers = selected
    # Reserve the search and every possible candidate read before parallel work.
    permitted, limited = reserve_stores(
        db, retailers, cost=1 + settings.discovery_candidates_per_store, custom=custom
    )
    with ThreadPoolExecutor(max_workers=settings.discovery_workers) as pool:
        discoveries = list(
            pool.map(
                lambda retailer: (
                    discover_store(
                        retailer,
                        query,
                        {**anchor.attributes, "_brand": anchor.brand, "_url": anchor.target.url},
                        custom=custom[retailer],
                    )
                    if retailer in custom
                    else discover_store(
                        retailer,
                        query,
                        {**anchor.attributes, "_brand": anchor.brand, "_url": anchor.target.url},
                    )
                ),
                permitted,
            )
        )
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
            record_attempt(db, failed["target"], started_at=failed["at"], error_code=failed["code"])
        db.commit()
        items = []
        seen_listings = set()
        for candidate in discovery["listings"]:
            if candidate.target.retailer != retailer:
                discovery["failures"].append(
                    "The search returned a listing from another retailer; it was excluded."
                )
                continue
            identity = candidate.target.external_id or candidate.target.url
            if identity in seen_listings:
                continue
            seen_listings.add(identity)
            decision = match_variant(anchor, candidate)
            model_decision = match_model(anchor, candidate)
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
                        "image_url": candidate.image_url,
                        "rating": candidate.rating,
                        "url": candidate.target.url,
                        "item_paise": candidate.item_paise,
                        "shipping_paise": candidate.shipping_paise,
                        "availability": candidate.availability,
                        "condition": candidate.condition,
                        "match_status": model_decision["status"],
                        "reason": model_decision["reason"],
                        "differences": model_decision.get("differences", []),
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
        models = sum(item["match_status"] == "model_match" for item in items)
        stores.append(
            {
                "retailer": retailer,
                "retailer_name": custom[retailer]["name"]
                if retailer in custom
                else STORES[retailer][0],
                "status": "verified"
                if verified
                else "model_match"
                if models
                else "review"
                if any(item["match_status"] == "review" for item in items)
                else "different_variant"
                if items
                else search["status"]
                if search["status"] != "ok"
                else "no_verified_match",
                "message": f"{verified} matching offer(s) joined the comparison."
                if verified
                else f"{models} same-model offer(s) found. Variant histories stay separate."
                if models
                else "Possible matches have separate histories until their variant is verified."
                if any(item["match_status"] == "review" for item in items)
                else "These readable listings are different variants and are excluded from the comparison."
                if items
                else (
                    discovery["failures"][0]
                    if discovery["failures"]
                    else "This search returned no readable candidates. The store may still stock this product."
                    if search["status"] == "no_matches"
                    else search.get("message", "No readable candidate listing was returned.")
                    if search["status"] != "ok"
                    else "No verified offer for this exact variant was readable."
                ),
                "search_url": search["search_url"],
                "items": items,
            }
        )
    result = jsonable_encoder(
        {
            "product_id": product_id,
            "stores": stores,
            "checked_at": now().isoformat(),
            "cached": False,
            "history_note": "Historical prices come from actual recorded or imported observations. A current page does not expose every past price.",
            "source_ids": sorted(source_ids),
            "discovery_retailers": retailers,
            "discovery_version": DISCOVERY_VERSION,
            "candidates_per_store": settings.discovery_candidates_per_store,
            "model_offers": model_offers(db, product_id, anchor),
        }
    )
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
