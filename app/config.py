from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRICELEDGER_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./priceledger.db"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    admin_token: str = ""
    demo_mode: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
