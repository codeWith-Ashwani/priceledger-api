from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.analysis import router as analysis_router
from app.auth import require_admin
from app.catalog import router
from app.comparison import router as comparison_router
from app.config import get_settings
from app.db import get_db
from app.discovery import router as discovery_router
from app.history_imports import router as imports_router
from app.http_safety import RequestSafetyMiddleware
from app.ingestion import IngestionError, ingest
from app.insights import router as insights_router
from app.lookup import router as lookup_router
from app.operations import router as operations_router
from app.schemas import IngestBatch

settings = get_settings()
app = FastAPI(title="PriceLedger API", version="0.1.0")
app.add_middleware(RequestSafetyMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Admin-Token"],
    expose_headers=["X-Request-ID", "Retry-After"],
)
app.include_router(router)
app.include_router(analysis_router)
app.include_router(insights_router)
app.include_router(lookup_router)
app.include_router(comparison_router)
app.include_router(imports_router)
app.include_router(discovery_router)
app.include_router(operations_router)


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    # FastAPI's default includes submitted values. Keep private import evidence out
    # of validation responses while preserving field locations and useful messages.
    return JSONResponse(
        {"detail": [{key: error[key] for key in ("loc", "msg", "type")} for error in exc.errors()]},
        status_code=422,
    )


@app.get("/api/v1/ready")
def ready(db: Session = Depends(get_db)):
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parent.parent / "migrations")
    )
    expected = set(ScriptDirectory.from_config(config).get_heads())
    try:
        db.execute(text("SELECT 1"))
        actual = set(MigrationContext.configure(db.connection()).get_current_heads())
        if actual != expected:
            raise HTTPException(
                503, "Database migrations are not current", headers={"Retry-After": "5"}
            )
    except SQLAlchemyError as exc:
        raise HTTPException(
            503, "Database temporarily unavailable", headers={"Retry-After": "5"}
        ) from exc
    return {"status": "ready", "service": "priceledger-api"}


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
