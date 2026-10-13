"""Collector container readiness; collection progress is reported separately."""

from app.db import SessionLocal
from app.main import ready

if __name__ == "__main__":
    with SessionLocal() as db:
        ready(db)
