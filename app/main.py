from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings

settings = get_settings()
app = FastAPI(title="PriceLedger API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Admin-Token"],
)


@app.get("/api/v1/health")
def health() -> dict[str, str | bool]:
    return {"status": "ok", "service": "priceledger-api", "demo_mode": settings.demo_mode}
