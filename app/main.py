import hmac

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.catalog import router
from app.analysis import router as analysis_router
from app.config import get_settings
from app.db import get_db
from app.ingestion import IngestionError, ingest
from app.schemas import IngestBatch

settings = get_settings()
app = FastAPI(title="PriceLedger API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Admin-Token"],
)
app.include_router(router)
app.include_router(analysis_router)


@app.get("/api/v1/health")
def health() -> dict[str, str | bool]:
    return {"status": "ok", "service": "priceledger-api", "demo_mode": settings.demo_mode}


@app.post("/api/v1/ingest")
def import_observations(
    batch: IngestBatch,
    x_admin_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    if not settings.admin_token:
        raise HTTPException(403, "Administrative ingestion is disabled")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(401, "Invalid administrative credentials")
    try:
        return ingest(db, batch, demo_mode=settings.demo_mode)
    except IngestionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
