from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, now


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    history_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    display_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_reference: Mapped[str | None] = mapped_column(String(1000))


class Product(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    title: Mapped[str] = mapped_column(String(250))
    brand: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(80), index=True)
    variant_label: Mapped[str] = mapped_column(String(200))
    attributes: Mapped[dict] = mapped_column(JSON)
    illustration: Mapped[str] = mapped_column(String(40))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    image_url: Mapped[str | None] = mapped_column(String(2048))


class Retailer(Base):
    __tablename__ = "retailers"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    color: Mapped[str] = mapped_column(String(10))


class Offer(Base):
    __tablename__ = "offers"
    __table_args__ = (
        UniqueConstraint("retailer_id", "external_id", "context_key"),
        Index("ix_offers_product", "product_id"),
    )
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    retailer_id: Mapped[str] = mapped_column(ForeignKey("retailers.id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    external_id: Mapped[str] = mapped_column(String(150))
    context_key: Mapped[str] = mapped_column(String(150), default="IN:new:standard")
    seller: Mapped[str] = mapped_column(String(150))
    condition: Mapped[str] = mapped_column(String(40), default="new")
    region: Mapped[str] = mapped_column(String(80), default="IN")
    current_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_item_paise: Mapped[int | None] = mapped_column(Integer)
    current_shipping_paise: Mapped[int | None] = mapped_column(Integer)
    current_in_stock: Mapped[bool] = mapped_column(Boolean, default=False)
    product_url: Mapped[str | None] = mapped_column(String(2048))
    channel: Mapped[str] = mapped_column(String(20), default="online", server_default="online")
    availability: Mapped[str] = mapped_column(
        String(20), default="unknown", server_default="unknown"
    )
    rating: Mapped[dict | None] = mapped_column(JSON)
    current_listed_fees: Mapped[list | None] = mapped_column(JSON)


class Observation(Base):
    __tablename__ = "observations"
    __table_args__ = (
        UniqueConstraint("source_id", "external_event_id"),
        CheckConstraint("item_paise IS NULL OR item_paise >= 0"),
        CheckConstraint("shipping_paise IS NULL OR shipping_paise >= 0"),
        CheckConstraint("mrp_paise IS NULL OR mrp_paise >= 0"),
        Index("ix_observations_offer_time", "offer_id", "observed_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offer_id: Mapped[str] = mapped_column(ForeignKey("offers.id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    external_event_id: Mapped[str] = mapped_column(String(160))
    fingerprint: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    item_paise: Mapped[int | None] = mapped_column(Integer)
    shipping_paise: Mapped[int | None] = mapped_column(Integer)
    mrp_paise: Mapped[int | None] = mapped_column(Integer)
    in_stock: Mapped[bool] = mapped_column(Boolean)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    variant_snapshot: Mapped[dict | None] = mapped_column(JSON)
    context_snapshot: Mapped[dict | None] = mapped_column(JSON)
    evidence_kind: Mapped[str] = mapped_column(
        String(30), default="recorded", server_default="recorded"
    )
    import_id: Mapped[str | None] = mapped_column(ForeignKey("history_imports.id"))
    evidence_reference: Mapped[str | None] = mapped_column(String(1000))
    price_type: Mapped[str] = mapped_column(String(30), default="listing", server_default="listing")
    listed_fees: Mapped[list | None] = mapped_column(JSON)


class DiscoveryStore(Base):
    __tablename__ = "discovery_stores"
    retailer_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), unique=True)
    host: Mapped[str] = mapped_column(String(253), unique=True)
    brands: Mapped[list] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    learned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProductMatch(Base):
    __tablename__ = "product_matches"
    left_id: Mapped[str] = mapped_column(ForeignKey("products.id"), primary_key=True)
    right_id: Mapped[str] = mapped_column(ForeignKey("products.id"), primary_key=True)
    method: Mapped[str] = mapped_column(String(40))
    evidence: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ComparisonScan(Base):
    __tablename__ = "comparison_scans"
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), primary_key=True)
    result: Mapped[dict] = mapped_column(JSON)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CollectionJob(Base):
    __tablename__ = "collection_jobs"
    __table_args__ = (Index("ix_collection_jobs_due", "status", "next_run_at"),)
    offer_id: Mapped[str] = mapped_column(ForeignKey("offers.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(60))


class CollectionAttempt(Base):
    __tablename__ = "collection_attempts"
    __table_args__ = (Index("ix_collection_attempts_offer_time", "offer_id", "started_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offer_id: Mapped[str | None] = mapped_column(ForeignKey("offers.id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    product_url: Mapped[str] = mapped_column(String(2048))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(20), default="running")
    error_code: Mapped[str | None] = mapped_column(String(60))


class CollectionBudget(Base):
    __tablename__ = "collection_budgets"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    budget_day: Mapped[str] = mapped_column(String(10))
    reserved_requests: Mapped[int] = mapped_column(Integer, default=0)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HistoryImport(Base):
    __tablename__ = "history_imports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    file_name: Mapped[str] = mapped_column(String(150))
    content_sha256: Mapped[str] = mapped_column(String(64))
    mapping_digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSON)


class SourceOfferMapping(Base):
    __tablename__ = "source_offer_mappings"
    __table_args__ = (UniqueConstraint("source_id", "retailer_id", "retailer_listing_id"),)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    offer_id: Mapped[str] = mapped_column(ForeignKey("offers.id"), primary_key=True)
    retailer_id: Mapped[str] = mapped_column(ForeignKey("retailers.id"))
    retailer_listing_id: Mapped[str] = mapped_column(String(150))
    variant_snapshot: Mapped[dict] = mapped_column(JSON)
    context_snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ClientQuota(Base):
    __tablename__ = "client_quotas"
    __table_args__ = (
        CheckConstraint("requests >= 1", name="ck_client_quotas_positive"),
        Index("ix_client_quotas_expires", "expires_at"),
    )
    client_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    bucket: Mapped[str] = mapped_column(String(20), primary_key=True)
    requests: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ComparisonLease(Base):
    __tablename__ = "comparison_leases"
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), primary_key=True)
    token: Mapped[str] = mapped_column(String(36))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
