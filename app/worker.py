"""Bounded collection of already-tracked real listings, with explicit retailer failures."""

import argparse
import time
from datetime import timedelta

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, now
from app.lookup import lookup_link
from app.models import Offer, Product, Source
from app.retail import LookupError, target_for


def refresh_once():
    blocked_stores = set()
    with SessionLocal() as db:
        urls = list(
            db.scalars(
                select(Offer.product_url)
                .join(Product)
                .join(Source)
                .where(
                    Product.is_demo.is_(False),
                    Source.kind == "public_page",
                    Source.display_allowed.is_(True),
                    Source.history_allowed.is_(True),
                    (Source.expires_at.is_(None) | (Source.expires_at > now())),
                    Offer.product_url.is_not(None),
                    (
                        Offer.current_observed_at.is_(None)
                        | (
                            Offer.current_observed_at
                            <= now() - timedelta(minutes=get_settings().lookup_cache_minutes)
                        )
                    ),
                )
                .order_by(Offer.current_observed_at.asc().nullsfirst())
                .limit(10)
            )
        )
        for url in urls:
            retailer = target_for(url).retailer
            if retailer in blocked_stores:
                continue
            try:
                result = lookup_link(db, url)
                print(f"Recorded: {result['product_id']}")
            except LookupError as exc:
                db.rollback()
                print(f"Unavailable: {exc.code}")
                if exc.code in {"retailer_blocked", "connection_failed", "retailer_unavailable"}:
                    blocked_stores.add(retailer)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Refresh real tracked retailer listings")
    parser.add_argument("--once", action="store_true", help="Perform one refresh pass and exit")
    parser.add_argument("--interval-minutes", type=int, default=15)
    args = parser.parse_args()
    if args.interval_minutes < 15:
        parser.error("The refresh interval must be at least 15 minutes")
    while True:
        refresh_once()
        if args.once:
            break
        time.sleep(args.interval_minutes * 60)
