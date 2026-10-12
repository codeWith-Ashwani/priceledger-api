"""PostgreSQL races run only against the disposable migrated integration database."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal, utc
from app.lookup import store_listing
from app.models import Offer, OfferReport, ReportEvent
from app.reports import ReportInput, ReviewInput, review, submit
from app.retail import Listing, target_for

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="Requires disposable PostgreSQL integration database",
)


def test_concurrent_submission_and_review_preserve_one_receipt_and_one_revision():
    listing = Listing(
        target_for("https://report-race.example.org/products/contract"),
        "Report race model",
        "Contract",
        "electronics",
        {"model": "Report race"},
        None,
        10000,
        None,
        "in_stock",
        "new",
        "Contract seller",
    )
    with SessionLocal() as db:
        product_id = store_listing(db, listing)
        offer = db.get(Offer, product_id + ":public-page")
        body = ReportInput(
            request_id=uuid4(),
            offer_id=offer.id,
            comparison_product_id=product_id,
            reason="wrong_price",
            details="Controlled integration report",
            observed_at=utc(offer.current_observed_at),
            item_paise=offer.current_item_paise,
            in_stock=offer.current_in_stock,
        )

    def create(_):
        with SessionLocal() as db:
            return submit(body, Response(), db)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert len({result["report_id"] for result in results}) == 1
    assert sorted(result["replayed"] for result in results) == [False, True]
    report_id = results[0]["report_id"]

    def moderate(status):
        with SessionLocal() as db:
            try:
                review(
                    report_id,
                    ReviewInput(
                        expected_revision=1, status=status, note="Controlled owner investigation"
                    ),
                    db,
                )
                return 200
            except HTTPException as error:
                return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(moderate, ["in_review", "dismissed"]))
    assert sorted(statuses) == [200, 409]
    with SessionLocal() as db:
        assert db.get(OfferReport, report_id).revision == 2
        assert (
            db.scalar(
                select(func.count())
                .select_from(ReportEvent)
                .where(ReportEvent.report_id == report_id)
            )
            == 2
        )
        assert db.get(Offer, body.offer_id).current_item_paise == 10000
