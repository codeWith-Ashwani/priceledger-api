"""Offline quote races use only the disposable PostgreSQL CI database."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal, now
from app.lookup import store_listing
from app.models import OfflineQuote, OfflineQuoteEvent
from app.offline import (
    QuoteInput,
    ReviewInput,
    ShopInput,
    create_quote,
    create_shop,
    public_quotes,
    review_quote,
)
from app.retail import Listing, target_for

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="Requires disposable PostgreSQL integration database",
)


def setup_quote():
    identifier = uuid4().hex
    listing = Listing(
        target_for(f"https://offline-race.example.org/products/{identifier}"),
        "Offline concurrency contract",
        "Contract",
        "electronics",
        {"model": identifier},
        None,
        10000,
        None,
        "in_stock",
        "new",
        "Contract seller",
    )
    with SessionLocal() as db:
        product_id = store_listing(db, listing)
    shop_body = ShopInput(
        request_id=uuid4(),
        name=f"Contract {identifier}",
        city="Contract City",
        state="Contract State",
        area="Contract Market",
        approval_reference="CI only permission",
        rights_confirmed=True,
    )

    def register(_):
        with SessionLocal() as db:
            return create_shop(shop_body, Response(), db)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register, range(2)))
    assert sorted(r["replayed"] for r in results) == [False, True]
    at = now() - timedelta(minutes=1)
    body = QuoteInput(
        request_id=uuid4(),
        product_id=product_id,
        shop_id=shop_body.request_id,
        variant_attributes={"model": identifier},
        item_paise=10000,
        condition="new",
        in_stock=True,
        observed_at=at,
        valid_until=at + timedelta(days=2),
        evidence_reference="CI only evidence",
        exact_variant_confirmed=True,
        includes_tax=True,
        unconditional_price=True,
    )

    def record(_):
        with SessionLocal() as db:
            return create_quote(body, Response(), db)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(record, range(2)))
    assert sorted(r["replayed"] for r in results) == [False, True]
    return str(body.request_id)


def test_same_review_retry_commits_one_revision_and_receipt():
    quote_id = setup_quote()
    body = ReviewInput(
        request_id=uuid4(),
        expected_revision=1,
        action="publish",
        note="CI owner verified evidence",
    )

    def publish(_):
        with SessionLocal() as db:
            return review_quote(quote_id, body, db)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, range(2)))
    assert sorted(r["replayed"] for r in results) == [False, True]
    with SessionLocal() as db:
        assert db.get(OfflineQuote, quote_id).revision == 2
        result = public_quotes(
            db.get(OfflineQuote, quote_id).product_id,
            city_id=None,
            condition="new",
            days=90,
            limit=10,
            offset=0,
            db=db,
        )
        assert result["summary"]["current_shop_count"] == 1
        assert result["items"][0]["current"]
        assert (
            db.scalar(
                select(func.count())
                .select_from(OfflineQuoteEvent)
                .where(OfflineQuoteEvent.quote_id == quote_id)
            )
            == 2
        )


def test_competing_publish_withdrawal_cannot_both_change_one_revision():
    quote_id = setup_quote()

    def moderate(action):
        with SessionLocal() as db:
            try:
                review_quote(
                    quote_id,
                    ReviewInput(
                        request_id=uuid4(),
                        expected_revision=1,
                        action=action,
                        note="CI owner checked publication",
                    ),
                    db,
                )
                return 200
            except HTTPException as error:
                return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(moderate, ["publish", "withdraw"]))
    assert sorted(statuses) == [200, 409]
    with SessionLocal() as db:
        assert db.get(OfflineQuote, quote_id).revision == 2
        assert (
            db.scalar(
                select(func.count())
                .select_from(OfflineQuoteEvent)
                .where(OfflineQuoteEvent.quote_id == quote_id)
            )
            == 2
        )
