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
