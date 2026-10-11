from datetime import UTC, datetime, timedelta

from app.db import now
from app.evidence import daily_evidence
from app.insights import summarize
from app.models import Observation, Offer, Retailer, Source


def series(points, *, source="own", channel="online", price_type="listing", offer="a"):
    return [
        (
            Observation(
                observed_at=at,
                item_paise=price,
                in_stock=price is not None,
                evidence_kind="recorded",
                price_type=price_type,
            ),
            Offer(id=offer, source_id="own", channel=channel),
            Retailer(name=offer),
            Source(id=source, name=source),
        )
        for at, price in points
    ]


def test_daily_best_average_is_not_biased_by_poll_frequency_and_uses_india_days():
    at = datetime(2026, 10, 1, 18, 29, tzinfo=UTC)
    rows = [
        *series([(at, 100), (at + timedelta(minutes=1), 300), (at + timedelta(minutes=2), 500)]),
        *series([(at, 1)], source="archive"),
        *series([(at, 200)], offer="b"),
        *series([(at, 1)], channel="offline"),
        *series([(at, 1)], price_type="coupon"),
    ]
    result = summarize(daily_evidence(rows, "item"), "p", 30, "item", None)
    assert result["observed_days"] == 2
    assert result["daily_average_paise"] == 200
    assert result["low"]["price_paise"] == 100
    assert result["high"]["price_paise"] == 500
    assert result["observations"] == 4


def add(db, price, *, source="test", offer="offer-128", days=1, kind="recorded", context=None):
    db.add(
        Observation(
            offer_id=offer,
            source_id=source,
            external_event_id=f"{source}-{offer}-{days}-{price}",
            fingerprint="x" * 64,
            observed_at=now() - timedelta(days=days),
            item_paise=price,
            shipping_paise=None,
            in_stock=True,
            evidence_kind=kind,
            context_snapshot=context,
        )
    )
    db.commit()


def test_insights_excludes_revoked_sources_wrong_variants_and_incompatible_imports(client, db):
    db.add(
        Source(
            id="archive",
            name="Archive",
            kind="historical",
            history_allowed=True,
            display_allowed=True,
        )
    )
    db.add(
        Offer(
            id="offer-256",
            product_id="phone-256",
            retailer_id="shop",
            source_id="test",
            external_id="external-256",
            seller="Merchant",
        )
    )
    db.commit()
    add(db, 10000)
    add(db, 1, source="blocked")
    add(db, 2, offer="offer-256")
    add(
        db,
        3,
        source="archive",
        days=2,
        kind="historical_import",
        context={"seller": "Wrong", "condition": "new", "region": "IN"},
    )
    result = client.get("/api/v1/products/phone-128/insights?basis=item").json()
    assert result["observations"] == 1
    assert result["low"]["price_paise"] == 10000
    assert result["current"]["price_paise"] == 10000
    assert result["low"]["source_name"] == "Approved test source"
    assert result["scope"] == "exact_variant"
    assert client.get("/api/v1/products/phone-128").json()["offers"][0]["variant_attributes"] == {
        "storage_gb": 128
    }
    db.get(Source, "test").history_allowed = False
    db.commit()
    assert client.get("/api/v1/products/phone-128/insights").json()["low"] is None


def test_selected_range_empty_history_and_unknown_shipping_are_explicit(client, db):
    add(db, 9000, days=60)
    short = client.get("/api/v1/products/phone-128/insights?days=30").json()
    assert short["observed_days"] == 0 and short["daily_average_paise"] is None
    long = client.get("/api/v1/products/phone-128/insights?days=90").json()
    assert long["low"]["price_paise"] == 9000
    delivered = client.get("/api/v1/products/phone-128/insights?basis=delivered").json()
    assert delivered["low"] is None
    assert delivered["current"]["price_paise"] == 10500
    assert client.get("/api/v1/products/phone-128/insights?days=0").status_code == 422
    assert client.get("/api/v1/products/unknown/insights").status_code == 404


def test_approved_archive_fills_missing_days_but_never_overweights_own_checks(client, db):
    archive = Source(
        id="archive", name="Archive", kind="historical", history_allowed=True, display_allowed=True
    )
    db.add(archive)
    db.commit()
    add(db, 10000)
    context = {"seller": "Merchant", "condition": "new", "region": "IN"}
    add(db, 9000, source="archive", kind="historical_import", context=context)
    add(db, 8000, source="archive", days=2, kind="historical_import", context=context)
    result = client.get("/api/v1/products/phone-128/insights").json()
    assert result["low"]["price_paise"] == 8000
    assert result["high"]["price_paise"] == 10000
    assert result["daily_average_paise"] == 9000
    assert result["observations"] == 2
    archive.expires_at = now() - timedelta(seconds=1)
    db.commit()
    result = client.get("/api/v1/products/phone-128/insights").json()
    assert result["observations"] == 1
    assert result["low"]["price_paise"] == 10000
