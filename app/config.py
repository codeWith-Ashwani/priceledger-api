from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRICELEDGER_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./priceledger.db"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    admin_token: str = ""
    demo_mode: bool = False
    discovery_retailers: list[
        Literal["amazon", "flipkart", "meesho", "croma", "reliance", "iqoo", "sony", "boat"]
    ] = [
        "amazon",
        "flipkart",
        "meesho",
        "croma",
        "reliance",
        "iqoo",
        "sony",
        "boat",
    ]
    lookup_cache_minutes: int = Field(default=15, ge=1, le=1440)
    comparison_cache_minutes: int = Field(default=15, ge=1, le=1440)
    discovery_workers: int = Field(default=3, ge=1, le=8)
    discovery_candidates_per_store: int = Field(default=2, ge=1, le=5)
    discovery_registry_capacity: int = Field(default=64, ge=1, le=1000)
    discovery_brands_per_store: int = Field(default=32, ge=1, le=256)
    discovery_extra_store_limit: int = Field(default=-1, ge=-1)
    lookup_requests_per_minute: int = Field(default=12, ge=1, le=1000)
    read_requests_per_minute: int = Field(default=120, ge=1, le=10_000)
    admin_requests_per_minute: int = Field(default=60, ge=1, le=1000)
    request_body_bytes: int = Field(default=1_500_000, ge=4096, le=5_000_000)
    request_body_timeout_seconds: int = Field(default=15, ge=1, le=60)
    comparison_lease_seconds: int = Field(default=600, ge=300, le=1800)
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=5, ge=0, le=50)
    database_pool_timeout_seconds: int = Field(default=10, ge=1, le=60)
    database_statement_timeout_ms: int = Field(default=30_000, ge=1000, le=120_000)
    database_lock_timeout_ms: int = Field(default=5000, ge=100, le=30_000)
    freshness_hours: int = Field(default=48, ge=1, le=168)
    history_gap_hours: int = Field(default=48, ge=1, le=168)
    max_history_observations: int = Field(default=10_000, ge=1000, le=100_000)
    collection_daily_budget: int = Field(default=500, ge=1, le=1_000_000)
    collection_active_hours: int = Field(default=6, ge=1, le=48)
    collection_stable_hours: int = Field(default=24, ge=1, le=168)
    collection_lease_minutes: int = Field(default=5, ge=1, le=60)
    collection_cooldown_hours: int = Field(default=6, ge=1, le=48)


@lru_cache
def get_settings() -> Settings:
    return Settings()
