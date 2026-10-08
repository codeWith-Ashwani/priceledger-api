from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db, now
from app.main import app
from app.models import Offer, Product, Retailer, Source


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add_all(
            [
                Source(
                    id="test",
                    name="Approved test source",
                    kind="merchant",
                    history_allowed=True,
                    display_allowed=True,
                ),
                Source(
                    id="blocked",
                    name="Unapproved",
                    kind="merchant",
                    history_allowed=False,
                    display_allowed=True,
                ),
                Retailer(id="shop", name="Test shop", color="#155c45"),
                Product(
                    id="phone-128",
                    title="Phone",
                    brand="Test",
                    category="electronics",
                    variant_label="128 GB",
                    attributes={"storage_gb": 128},
                    illustration="headphones",
                    is_demo=False,
                ),
                Product(
                    id="phone-256",
                    title="Phone",
                    brand="Test",
                    category="electronics",
                    variant_label="256 GB",
                    attributes={"storage_gb": 256},
                    illustration="headphones",
                    is_demo=False,
                ),
            ]
        )
        session.flush()
        session.add(
            Offer(
                id="offer-128",
                product_id="phone-128",
                retailer_id="shop",
                source_id="test",
                external_id="external-128",
                seller="Merchant",
                current_in_stock=True,
                current_item_paise=10_000,
                current_shipping_paise=500,
                current_observed_at=now() - timedelta(hours=1),
            )
        )
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
