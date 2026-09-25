from functools import lru_cache
from uuid import UUID

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://reservation:reservation@127.0.0.1:5454/reservation"
    reservation_ttl_seconds: int = 900

    external_provider_id: UUID | None = None
    external_provider_base_url: str | None = None
    external_provider_api_key: str | None = None
    external_provider_hold_timeout_seconds: float = 2.0

    provider_worker_poll_interval_seconds: float = Field(default=1.0, gt=0)
    provider_worker_batch_size: int = Field(default=500, gt=0, le=1_000)
    provider_worker_lease_seconds: int = Field(default=60, gt=0, le=3_600)
    provider_worker_concurrency: int = Field(default=5, gt=0, le=100)

    fake_provider_mode: str = "success"
    fake_provider_timeout_seconds: float = 5.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
