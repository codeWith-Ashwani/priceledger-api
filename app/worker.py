"""Run bounded durable jobs; network calls never hold a job transaction."""

import argparse
import time

from sqlalchemy import update

from app.collection import bootstrap_jobs, cadence, claim_job, retry_job
from app.db import SessionLocal, now
from app.lookup import store_listing
from app.models import CollectionAttempt, CollectionJob
from app.retail import LookupError, load_listing, target_for


def settle_job(db, claim: dict, *, listing=None, error_code: str | None = None) -> bool:
    at = now()
    # Fence expired workers before they can append a quote.
    owned = db.execute(
        update(CollectionJob)
        .execution_options(synchronize_session="fetch")
        .where(
            CollectionJob.offer_id == claim["offer_id"],
            CollectionJob.status == "running",
            CollectionJob.lease_token == claim["token"],
            CollectionJob.lease_expires_at > at,
        )
        .values(status="queued", lease_token=None, lease_expires_at=None)
    ).rowcount
    if not owned:
        db.rollback()
        return False
    job = db.get(CollectionJob, claim["offer_id"])
    attempt = db.get(CollectionAttempt, claim["attempt_id"])
    if error_code:
        retry_job(db, job, error_code)
        attempt.outcome, attempt.error_code = "failure", error_code
    else:
        if (
            f"{listing.target.retailer}:{listing.target.external_id}:public-page"
            != claim["offer_id"]
        ):
            raise LookupError(
                "variant_changed",
                "The refreshed listing no longer matches the tracked identity.",
                409,
            )
        store_listing(db, listing, commit=False, manage_job=False)
        job.consecutive_failures = 0
        job.last_error_code = None
        job.last_success_at = at
        job.next_run_at = at + cadence(job)
        attempt.outcome = "success"
    attempt.completed_at = now()
    db.commit()
    return True


def refresh_once(max_jobs: int = 10) -> int:
    with SessionLocal() as db:
        bootstrap_jobs(db)
    processed = 0
    for _ in range(max_jobs):
        with SessionLocal() as db:
            claim = claim_job(db)
        if claim is None:
            break
        error_code = None
        try:
            listing = load_listing(target_for(claim["url"]))
            with SessionLocal() as db:
                settled = settle_job(db, claim, listing=listing)
        except LookupError as exc:
            error_code = exc.code
        except Exception:
            error_code = "collector_error"
        if error_code:
            with SessionLocal() as db:
                settled = settle_job(db, claim, error_code=error_code)
        processed += 1
        print(
            f"{claim['offer_id']}: {error_code or 'recorded'}"
            if settled
            else "Expired lease: discarded result"
        )
    return processed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Refresh real tracked retailer listings")
    parser.add_argument("--once", action="store_true", help="Perform one bounded pass and exit")
    parser.add_argument("--interval-minutes", type=int, default=15)
    parser.add_argument("--max-jobs", type=int, default=10)
    args = parser.parse_args()
    if args.interval_minutes < 1 or not 1 <= args.max_jobs <= 100:
        parser.error("Use an interval of at least 1 minute and 1–100 jobs per pass")
    while True:
        refresh_once(args.max_jobs)
        if args.once:
            break
        time.sleep(args.interval_minutes * 60)
