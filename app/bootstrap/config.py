import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://reservation:reservation@127.0.0.1:5454/reservation",
    )
    reservation_ttl_seconds = int(os.getenv("RESERVATION_TTL_SECONDS", 600))
    external_provider_id = os.getenv("EXTERNAL_PROVIDER_ID")
    query_only_provider_id = os.getenv("QUERY_ONLY_PROVIDER_ID")
    mock_provider_mode = os.getenv("MOCK_PROVIDER_MODE", "success")
    mock_provider_available_quantity = os.getenv("MOCK_PROVIDER_AVAILABLE_QUANTITY", 100)
    provider_worker_poll_interval_seconds = os.getenv(
        "PROVIDER_WORKER_POLL_INTERVAL_SECONDS", 10.0
    )
    provider_worker_batch_size = os.getenv("PROVIDER_WORKER_BATCH_SIZE", 500)
    provider_worker_lease_seconds = os.getenv("PROVIDER_WORKER_LEASE_SECONDS", 60)
    provider_worker_concurrency = os.getenv("PROVIDER_WORKER_CONCURRENCY", 5)


@lru_cache
def get_settings() -> Settings:
    return Settings()
