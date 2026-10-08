"""Bounded housekeeping without touching price observations or import audit evidence."""

import argparse

from app.db import SessionLocal
from app.limits import prune_quotas


def main():
    parser = argparse.ArgumentParser(description="Remove old expired client quota windows")
    parser.add_argument("--max-rows", type=int, default=1000)
    args = parser.parse_args()
    if not 1 <= args.max_rows <= 10_000:
        parser.error("Use 1–10,000 rows per pass")
    with SessionLocal() as db:
        count = prune_quotas(db, args.max_rows)
    print(f"Removed {count} old expired client quota windows.")


if __name__ == "__main__":
    main()
