from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.analysis import router as analysis_router
from app.auth import require_admin
from app.catalog import router
from app.comparison import router as comparison_router
from app.config import get_settings
from app.db import get_db
from app.history_imports import router as imports_router
from app.ingestion import IngestionError, ingest
from app.lookup import router as lookup_router
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
app.include_router(lookup_router)
app.include_router(comparison_router)
app.include_router(imports_router)


@app.get("/api/v1/health")
def health() -> dict[str, str | bool]:
    return {"status": "ok", "service": "priceledger-api", "demo_mode": settings.demo_mode}


@app.post("/api/v1/ingest", dependencies=[Depends(require_admin)])
def import_observations(
    batch: IngestBatch,
    db: Session = Depends(get_db),
):
    try:
        return ingest(db, batch, demo_mode=settings.demo_mode)
    except IngestionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
