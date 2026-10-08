"""Explicit fictional sample data for exercising the application, never live retailer data."""
import argparse
import math
from datetime import timedelta

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, now
from app.ingestion import ingest
from app.models import Offer, Product, Retailer, Source
from app.schemas import IngestBatch, ObservationInput

SAMPLES = [
    ("auralis-studio", "Studio wireless headphones", "Auralis", "electronics", "Midnight · over-ear", {"model": "Studio", "color": "Midnight"}, "headphones", 14999),
    ("forma-chair", "Everyday ergonomic chair", "Forma", "home", "Sage · standard", {"model": "Everyday", "color": "Sage"}, "chair", 8499),
    ("stride-shoes", "Pace everyday sneakers", "Stride", "fashion", "Cream · UK 8", {"style": "Pace", "color": "Cream", "size": "UK 8"}, "shoe", 2499),
    ("field-coffee", "Single-origin ground coffee", "Field", "groceries", "Medium roast · 250 g", {"roast": "medium", "quantity_g": 250, "pack_count": 1}, "coffee", 449),
    ("leaf-serum", "Daily hydration serum", "Leaf", "beauty", "Hyaluronic acid · 30 ml", {"formulation": "hyaluronic acid", "quantity_ml": 30}, "serum", 699),
    ("open-notebook", "The quiet notebook", "Open", "books", "Hardcover · English", {"edition": "1", "binding": "hardcover", "language": "en"}, "book", 349),
]
RETAILERS = [("amazon", "Amazon", "#de9b3a"), ("flipkart", "Flipkart", "#4a79d8"), ("merchant", "Partner store", "#239673")]


def seed_demo() -> dict[str, int]:
    if not get_settings().demo_mode:
        raise SystemExit("Demo mode must be enabled before loading sample data")
    with SessionLocal() as db:
        if db.scalar(select(Source).where(Source.id == "sample-fixtures")):
            return {"products": 0, "observations": 0}
        db.add(Source(id="sample-fixtures", name="Synthetic development fixtures", kind="demo",
                      history_allowed=True, display_allowed=True))
        for retailer_id, name, color in RETAILERS:
            db.add(Retailer(id=retailer_id, name=name, color=color))
        for product_id, title, brand, category, variant, attributes, illustration, _ in SAMPLES:
            db.add(Product(id=product_id, title=title, brand=brand, category=category,
                           variant_label=variant, attributes=attributes, illustration=illustration, is_demo=True))
        db.flush()
        for product_id, *_ in SAMPLES:
            for retailer_id, _, _ in RETAILERS:
                db.add(Offer(id=f"{product_id}:{retailer_id}", product_id=product_id,
                    retailer_id=retailer_id, source_id="sample-fixtures", external_id=product_id,
                    seller="Fictional sample seller", condition="new", region="IN"))
        db.flush()
        records = []
        anchor = now().replace(microsecond=0)
        for product_index, sample in enumerate(SAMPLES):
            product_id, *_, base = sample
            attributes = sample[5]
            for retailer_index, (retailer_id, _, _) in enumerate(RETAILERS):
                for day in range(365, -1, -1):
                    if retailer_index == 1 and 43 <= day <= 47:
                        continue  # Deliberate missing coverage to verify graph gaps.
                    variation = math.sin((365 - day) / 21 + retailer_index) * .045
                    amount = base * (1.12 + day / 365 * .07 + variation + retailer_index * .025)
                    if day < 7:
                        amount = base * (1 + retailer_index * .04)
                    elif day < 14:
                        amount = base * (1.32 + retailer_index * .025)
                    if product_index == 0 and 50 <= day <= 85:
                        amount = base * (.98 + retailer_index * .03)
                    records.append(ObservationInput(offer_id=f"{product_id}:{retailer_id}",
                        external_event_id=f"{product_id}:{retailer_id}:{day}",
                        observed_at=anchor - timedelta(days=day), variant_attributes=attributes,
                        item_paise=round(amount) * 100, shipping_paise=0,
                        mrp_paise=round(base * 1.6) * 100, in_stock=not (retailer_index == 2 and day == 21)))
        count = 0
        for offset in range(0, len(records), 1000):
            count += ingest(db, IngestBatch(source_id="sample-fixtures", observations=records[offset:offset + 1000]), demo_mode=True, commit=False)["inserted"]
        db.commit()
        return {"products": len(SAMPLES), "observations": count}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load fictional PriceLedger sample data")
    parser.add_argument("--demo", action="store_true", required=True, help="Acknowledge that all prices are synthetic")
    parser.parse_args()
    print(seed_demo())
