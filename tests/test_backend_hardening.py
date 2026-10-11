import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.coordination import claim_comparison, fence_comparison, release_comparison
from app.db import Base, get_db, make_engine, now, utc
from app.http_safety import RequestSafetyMiddleware
from app.ingestion import IngestionError, ingest
from app.limits import prune_quotas, reserve_client
from app.main import app
from app.models import ClientQuota, CollectionAttempt, ComparisonLease, Observation, Offer, Product
from app.schemas import IngestBatch, ObservationInput


@pytest.fixture
def file_engine(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'hardening.db').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Product(
                id="concurrent-product",
                title="Concurrency contract",
                brand="Test",
                category="test",
                variant_label="test",
                attributes={"sku": "test"},
                illustration="product",
                is_demo=False,
            )
        )
        session.commit()
    yield engine
    engine.dispose()


def test_quota_is_shared_between_sessions_and_survives_failed_work(file_engine, monkeypatch):
    at = now()
    monkeypatch.setattr("app.limits.now", lambda: at)
    with Session(file_engine) as first:
        reserve_client(first, "client", "lookup", 1)
        first.rollback()
    with Session(file_engine) as second:
        with pytest.raises(HTTPException) as rejected:
            reserve_client(second, "client", "lookup", 1)
        assert rejected.value.status_code == 429
        assert rejected.value.headers == {"Retry-After": "60"}
        reserve_client(second, "client", "comparison", 1)
        reserve_client(second, "other-client", "lookup", 1)
        assert second.scalar(select(func.count()).select_from(ClientQuota)) == 3
        assert all(row.client_hash != "client" for row in second.scalars(select(ClientQuota)))


def test_atomic_quota_under_sqlite_concurrency(file_engine, monkeypatch):
    at = now()
    monkeypatch.setattr("app.limits.now", lambda: at)

    def attempt(_):
        with Session(file_engine) as session:
            try:
                reserve_client(session, "same-client", "lookup", 5)
                return True
            except HTTPException as exc:
                assert exc.status_code == 429
                return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(24))) == 5
    with Session(file_engine) as session:
        assert session.scalar(select(ClientQuota.requests)) == 5


def test_expired_quota_resets_and_retry_is_remaining_time(db, monkeypatch):
    at = now()
    monkeypatch.setattr("app.limits.now", lambda: at)
    reserve_client(db, "client", "read", 1)
    monkeypatch.setattr("app.limits.now", lambda: at + timedelta(seconds=25))
    with pytest.raises(HTTPException) as rejected:
        reserve_client(db, "client", "read", 1)
    assert rejected.value.headers["Retry-After"] == "35"
    monkeypatch.setattr("app.limits.now", lambda: at + timedelta(seconds=60))
    reserve_client(db, "client", "read", 1)
    assert db.scalar(select(ClientQuota.requests)) == 1


def test_forwarded_headers_cannot_bypass_quota(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "read_requests_per_minute", 1)
    assert client.get("/api/v1/products", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    rejected = client.get(
        "/api/v1/categories", headers={"X-Forwarded-For": "8.8.8.8", "Forwarded": "for=8.8.8.8"}
    )
    assert rejected.status_code == 429
    assert client.get("/api/v1/health").status_code == 200


def test_admin_quota_and_no_store_on_denial(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_token", "isolated-test-secret")
    monkeypatch.setattr(get_settings(), "admin_requests_per_minute", 1)
    headers = {"X-Admin-Token": "isolated-test-secret"}
    assert client.get("/api/v1/admin/sources", headers=headers).status_code == 200
    response = client.get("/api/v1/admin/imports", headers=headers)
    assert response.status_code == 429 and response.headers["Cache-Control"] == "no-store"
    invalid = client.get("/api/v1/admin/imports")
    assert invalid.status_code == 401 and invalid.headers["Cache-Control"] == "no-store"


def test_bounded_housekeeping_preserves_active_quotas_and_observations(db):
    for index in range(3):
        db.add(
            ClientQuota(
                client_hash=str(index),
                bucket="read",
                requests=1,
                expires_at=now() - timedelta(days=2),
            )
        )
    db.add(
        ClientQuota(
            client_hash="active",
            bucket="lookup",
            requests=2,
            expires_at=now() + timedelta(seconds=50),
        )
    )
    db.commit()
    assert prune_quotas(db, 2) == 2
    assert db.get(ClientQuota, ("active", "lookup")).requests == 2
    assert db.scalar(select(func.count()).select_from(ClientQuota)) == 2
    assert prune_quotas(db) == 1
    assert db.scalar(select(func.count()).select_from(Observation)) == 0
    with pytest.raises(ValueError):
        prune_quotas(db, 10001)


def test_comparison_lease_excludes_same_product_and_allows_others(db):
    first = claim_comparison(db, "phone-128")
    with pytest.raises(HTTPException) as busy:
        claim_comparison(db, "phone-128")
    assert busy.value.status_code == 409 and busy.value.headers["Retry-After"] == "15"
    other = claim_comparison(db, "phone-256")
    release_comparison(db, "phone-128", first)
    assert db.get(ComparisonLease, "phone-128") is None
    assert db.get(ComparisonLease, "phone-256").token == other


def test_sqlite_concurrent_comparison_claim_has_one_owner(file_engine):
    def attempt(_):
        with Session(file_engine) as session:
            try:
                return claim_comparison(session, "concurrent-product")
            except HTTPException as exc:
                assert exc.status_code == 409
                return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        winners = [token for token in pool.map(attempt, range(16)) if token]
    assert len(winners) == 1


def test_expired_comparison_cannot_publish_or_release_new_owner(db):
    old = claim_comparison(db, "phone-128")
    db.get(ComparisonLease, "phone-128").expires_at = now() - timedelta(seconds=1)
    db.commit()
    new = claim_comparison(db, "phone-128")
    with pytest.raises(HTTPException, match="409"):
        fence_comparison(db, "phone-128", old)
    release_comparison(db, "phone-128", old)
    assert db.get(ComparisonLease, "phone-128").token == new
    fence_comparison(db, "phone-128", new)
    db.rollback()


def test_comparison_route_releases_lease_on_failure(client, db, monkeypatch):
    def fail(*_, **__):
        raise RuntimeError("Private retailer response")

    monkeypatch.setattr("app.comparison.scan_comparison", fail)
    response = client.post("/api/v1/products/phone-128/compare")
    assert response.status_code == 500
    assert "Private" not in response.text
    assert db.get(ComparisonLease, "phone-128") is None


def test_busy_comparison_never_starts_discovery(client, db, monkeypatch):
    token = claim_comparison(db, "phone-128")

    def forbidden(*_, **__):
        pytest.fail("Busy product must not start retailer discovery")

    monkeypatch.setattr("app.comparison.scan_comparison", forbidden)
    response = client.post(
        "/api/v1/products/phone-128/compare", headers={"Origin": "http://127.0.0.1:5173"}
    )
    assert response.status_code == 409
    assert response.headers["Retry-After"] == "15"
    assert "Retry-After" in response.headers["Access-Control-Expose-Headers"]
    release_comparison(db, "phone-128", token)


def test_stale_quote_writer_cannot_replace_newer_persisted_price(db):
    stale = db.get(Offer, "offer-128")
    original = stale.current_observed_at
    # Another writer changes the DB while this session retains the old ORM identity.
    at = now()
    with Session(db.bind) as newer:
        offer = newer.get(Offer, "offer-128")
        offer.current_observed_at = at
        offer.current_item_paise = 12000
        newer.commit()
    assert stale.current_observed_at == original
    record = ObservationInput(
        offer_id="offer-128",
        external_event_id="delayed-writer",
        observed_at=at - timedelta(seconds=10),
        variant_attributes={"storage_gb": 128},
        item_paise=8000,
        shipping_paise=0,
        in_stock=True,
    )
    ingest(db, IngestBatch(source_id="test", observations=[record]), demo_mode=False)
    db.refresh(stale)
    assert stale.current_item_paise == 12000 and utc(stale.current_observed_at) == at
    assert db.scalar(select(func.count()).select_from(Observation)) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("item_paise", True),
        ("shipping_paise", "500"),
        ("mrp_paise", 500.0),
        ("in_stock", "true"),
        ("variant_attributes", {"storage_gb": float("nan")}),
        ("variant_attributes", {"storage_gb": "x" * 10001}),
    ],
)
def test_legacy_ingestion_rejects_coercion_and_unbounded_attributes(field, value):
    values = dict(
        offer_id="offer-128",
        external_event_id="event",
        observed_at=now(),
        variant_attributes={"storage_gb": 128},
        item_paise=10000,
        in_stock=True,
    )
    with pytest.raises(ValueError):
        ObservationInput(**{**values, field: value})


def test_legacy_ingestion_rejects_boolean_numeric_variant(db):
    record = ObservationInput(
        offer_id="offer-128",
        external_event_id="bool-variant",
        observed_at=now(),
        variant_attributes={"storage_gb": True},
        item_paise=10000,
        in_stock=True,
    )
    db.get(Product, "phone-128").attributes = {"storage_gb": 1}
    db.commit()
    with pytest.raises(IngestionError, match="variant"):
        ingest(db, IngestBatch(source_id="test", observations=[record]), demo_mode=False)


def test_validation_does_not_echo_private_body_and_cors_is_preserved(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_token", "isolated-test-secret")
    response = client.post(
        "/api/v1/admin/sources",
        json={
            "id": "archive",
            "name": "Private submitted evidence",
            "approval_reference": "Private invoice reference",
            "rights_confirmed": False,
        },
        headers={"X-Admin-Token": "isolated-test-secret", "Origin": "http://localhost:5173"},
    )
    assert response.status_code == 422
    assert "Private submitted" not in response.text and "Private invoice" not in response.text
    assert all("input" not in error and "ctx" not in error for error in response.json()["detail"])
    assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert "X-Request-ID" in response.headers["Access-Control-Expose-Headers"]


def test_request_ids_headers_and_safe_template_logs(client, caplog):
    with caplog.at_level("INFO", logger="priceledger.http"):
        response = client.get(
            "/api/v1/products/private-product-id?q=private-search",
            headers={"X-Request-ID": "untrusted-id", "X-Admin-Token": "private-token"},
        )
    assert response.status_code == 404
    UUID(response.headers["X-Request-ID"])
    assert response.headers["X-Request-ID"] != "untrusted-id"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "route=/api/v1/products/{product_id}" in caplog.text
    assert all(
        secret not in caplog.text
        for secret in ("private-product-id", "private-search", "private-token")
    )


async def exercise_middleware(messages, headers=(), path="/api/v1/lookup", fail=None):
    received, calls = [], []
    iterator = iter(messages)

    async def receive():
        message = next(iterator)
        if message == "slow":
            await asyncio.sleep(5)
        return message

    async def send(message):
        received.append(message)

    async def inner(scope, receive, send):
        calls.append(scope)
        if fail:
            raise fail
        body = await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body.get("body", b"")})

    scope = {"type": "http", "method": "POST", "path": path, "headers": list(headers), "state": {}}
    await RequestSafetyMiddleware(inner)(scope, receive, send)
    return received, calls


@pytest.mark.parametrize(
    "headers,status",
    [
        ([(b"content-length", b"5000")], 413),
        ([(b"content-length", b"-2")], 400),
        ([(b"content-length", b"1"), (b"content-length", b"1")], 400),
        ([(b"content-encoding", b"gzip")], 415),
    ],
)
def test_invalid_headers_rejected_before_reading(headers, status, monkeypatch):
    monkeypatch.setattr(get_settings(), "request_body_bytes", 4096)
    messages, called = asyncio.run(exercise_middleware([], headers))
    assert messages[0]["status"] == status and not called


def test_chunked_oversized_body_is_bounded_without_content_length(monkeypatch):
    monkeypatch.setattr(get_settings(), "request_body_bytes", 4096)
    chunks = [{"type": "http.request", "body": b"x" * 2000, "more_body": True}] * 3
    messages, called = asyncio.run(exercise_middleware(chunks))
    assert messages[0]["status"] == 413 and not called


def test_chunked_json_reaches_handler_unchanged():
    body = b'{"query":"brand model"}'
    chunks = [
        {"type": "http.request", "body": body[:10], "more_body": True},
        {"type": "http.request", "body": body[10:], "more_body": False},
    ]
    messages, called = asyncio.run(
        exercise_middleware(chunks, [(b"content-type", b"application/json")])
    )
    assert messages[0]["status"] == 200 and len(called) == 1 and messages[1]["body"] == body


def test_slow_body_deadline_and_disconnect_never_reach_handler(monkeypatch):
    monkeypatch.setattr(get_settings(), "request_body_timeout_seconds", 1)
    messages, called = asyncio.run(exercise_middleware(["slow"]))
    assert messages[0]["status"] == 408 and not called
    messages, called = asyncio.run(exercise_middleware([{"type": "http.disconnect"}]))
    assert not messages and not called


def test_admin_auth_precedes_reading_body_and_rejections_have_cors(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_token", "isolated-test-secret")
    messages, called = asyncio.run(exercise_middleware([], path="/api/v1/admin/imports/preview"))
    assert messages[0]["status"] == 401 and not called
    response = client.post(
        "/api/v1/ingest", content="private", headers={"Origin": "http://localhost:5173"}
    )
    assert response.status_code == 401
    assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"


@pytest.mark.parametrize(
    "body",
    [
        b'{"query":"first","query":"second"}',
        b'{"query":NaN}',
        b'{"query":Infinity}',
        b"\xff",
        b'{"query":',
        rb'{"query":"\ud800"}',
    ],
)
def test_invalid_json_does_not_reach_handler_or_echo_body(body):
    messages, called = asyncio.run(
        exercise_middleware(
            [{"type": "http.request", "body": body}], [(b"content-type", b"application/json")]
        )
    )
    assert messages[0]["status"] == 422 and not called
    assert b"first" not in messages[1]["body"]


@pytest.mark.parametrize(
    "exception,status",
    [
        (RuntimeError("private body"), 500),
        (
            OperationalError(
                "private SQL", {"secret": "private bound value"}, Exception("private database")
            ),
            503,
        ),
        (IntegrityError("private SQL", {}, Exception("private constraint")), 409),
    ],
)
def test_safe_failure_responses_and_logs(exception, status, caplog):
    with caplog.at_level("INFO", logger="priceledger.http"):
        messages, called = asyncio.run(
            exercise_middleware([{"type": "http.request", "body": b""}], fail=exception)
        )
    assert messages[0]["status"] == status and called
    assert "private" not in caplog.text and b"private" not in messages[1]["body"]
    assert dict(messages[0]["headers"])[b"x-request-id"]


def test_history_refuses_oversized_ranges_without_silent_truncation(client, db, monkeypatch):
    for index in range(3):
        db.add(
            Observation(
                offer_id="offer-128",
                source_id="test",
                external_event_id=f"bounded-{index}",
                fingerprint="test",
                observed_at=now() - timedelta(days=index + 1),
                item_paise=10000,
                shipping_paise=0,
                in_stock=True,
            )
        )
    db.commit()
    monkeypatch.setattr(get_settings(), "max_history_observations", 2)
    response = client.get("/api/v1/products/phone-128/history?days=90")
    assert response.status_code == 422 and "shorter period" in response.text
    result = client.get("/api/v1/products/phone-128/history?days=2").json()
    assert len(result["series"][0]["points"]) == 1


def test_history_failed_check_range_is_also_bounded(client, db, monkeypatch):
    for index in range(3):
        db.add(
            CollectionAttempt(
                offer_id="offer-128",
                source_id="test",
                product_url="https://store.example.org/test",
                started_at=now() - timedelta(hours=index + 1),
                completed_at=now(),
                outcome="failure",
                error_code="connection_failed",
            )
        )
    db.commit()
    monkeypatch.setattr(get_settings(), "max_history_observations", 2)
    response = client.get("/api/v1/products/phone-128/history")
    assert response.status_code == 422 and "too many checks" in response.text


def test_readiness_detects_missing_and_current_migrations(client, db):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    assert client.get("/api/v1/ready").status_code == 503
    config = Config("alembic.ini")
    head = ScriptDirectory.from_config(config).get_current_head()
    db.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
    db.execute(text("INSERT INTO alembic_version VALUES (:version)"), {"version": head})
    db.commit()
    assert client.get("/api/v1/ready").status_code == 200
    db.execute(text("UPDATE alembic_version SET version_num='6782e38bcd93'"))
    db.commit()
    assert client.get("/api/v1/ready").status_code == 503


def test_database_failure_does_not_break_liveness(client):
    def unavailable():
        raise OperationalError("private SQL", {}, Exception("private credentials"))
        yield  # pragma: no cover

    app.dependency_overrides[get_db] = unavailable
    assert client.get("/api/v1/health").status_code == 200
    response = client.get("/api/v1/ready")
    assert response.status_code == 503 and "private" not in response.text


def test_configuration_rejects_dangerous_bounds():
    for values in (
        {"lookup_requests_per_minute": 0},
        {"request_body_bytes": 100_000_000},
        {"database_pool_size": 0},
        {"database_statement_timeout_ms": 0},
        {"comparison_lease_seconds": 0},
    ):
        with pytest.raises(ValueError):
            Settings(_env_file=None, **values)
