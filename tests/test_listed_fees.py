from datetime import timedelta

import pytest
from bs4 import BeautifulSoup

from app.db import now
from app.ingestion import IngestionError, ingest
from app.models import Offer
from app.retail import listed_fees, target_for
from app.schemas import IngestBatch, ObservationInput

FEE = {"label": "Protect Promise Fee", "amount_paise": 3900, "evidence": "page_price_breakdown"}


def fee_link(pid="MOBTEST123", amount="39"):
    return (
        f'<a href="/pp-protect-promise-fee?pid={pid}"><div>+₹{amount} Protect Promise Fee</div></a>'
    )


@pytest.mark.parametrize(
    "html,expected",
    [
        (fee_link(), [FEE]),
        (fee_link() + fee_link(), [FEE]),
        (fee_link("OTHER123"), None),
        (fee_link() + fee_link(amount="49"), None),
        ("<p>₹39 with bank card</p>", None),
        (fee_link().replace('href="/', 'href="https://evil.example.org/'), None),
    ],
)
def test_only_unambiguous_pid_bound_extra_is_read(html, expected):
    target = target_for("https://www.flipkart.com/test/p/itm123?pid=MOBTEST123")
    assert listed_fees(BeautifulSoup(html, "html.parser"), target) == expected


def record(event, at, fees):
    return ObservationInput(
        offer_id="offer-128",
        external_event_id=event,
        observed_at=at,
        variant_attributes={"storage_gb": 128},
        item_paise=9000,
        shipping_paise=500,
        in_stock=True,
        listed_fees=fees,
    )


def test_fees_are_dated_current_subtotal_is_separate_and_missing_evidence_clears_quote(client, db):
    at = now() - timedelta(minutes=1)
    payload = IngestBatch(source_id="test", observations=[record("fee-1", at, [FEE])])
    ingest(db, payload, demo_mode=False)
    quote = client.get("/api/v1/products/phone-128").json()["offers"][0]
    assert quote["total_paise"] == 9500
    assert quote["known_subtotal_paise"] == 13400
    assert quote["listed_fees"] == [FEE]
    delayed = record("delayed", at - timedelta(seconds=10), None)
    ingest(db, IngestBatch(source_id="test", observations=[delayed]), demo_mode=False)
    assert db.get(Offer, "offer-128").current_listed_fees == [FEE]
    latest = record("latest", at + timedelta(seconds=10), None)
    ingest(db, IngestBatch(source_id="test", observations=[latest]), demo_mode=False)
    assert db.get(Offer, "offer-128").current_listed_fees is None
    points = client.get("/api/v1/products/phone-128/history").json()["series"][0]["points"]
    assert sum(point["listed_fees"] == [FEE] for point in points) == 1
    assert all(point["total_paise"] == 9500 for point in points)


def test_fee_evidence_participates_in_event_idempotency(db):
    at = now()
    payload = IngestBatch(source_id="test", observations=[record("same", at, [FEE])])
    assert ingest(db, payload, demo_mode=False)["inserted"] == 1
    assert ingest(db, payload, demo_mode=False)["duplicates"] == 1
    changed = IngestBatch(source_id="test", observations=[record("same", at, None)])
    with pytest.raises(IngestionError, match="reused"):
        ingest(db, changed, demo_mode=False)


def test_duplicate_admin_fee_labels_cannot_double_charge_subtotal():
    with pytest.raises(ValueError, match="unique"):
        record("duplicate", now(), [FEE, {**FEE, "label": "protect promise fee"}])
