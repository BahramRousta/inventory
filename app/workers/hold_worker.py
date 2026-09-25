import argparse
import asyncio

from app.application.services.claim_pending_provider_holds import (
    ClaimPendingProviderHoldsService,
)
from app.bootstrap.config import get_settings
from app.bootstrap.dependencies import get_process_pending_provider_hold_service
from app.infrastructure.db.session import AsyncSessionLocal
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork


async def run_once() -> bool:
    settings = get_settings()
    claimed = await ClaimPendingProviderHoldsService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(AsyncSessionLocal),
        batch_size=settings.provider_worker_batch_size,
        lease_seconds=settings.provider_worker_lease_seconds,
    ).execute()
    if not claimed:
        return False
    processor = get_process_pending_provider_hold_service()
    semaphore = asyncio.Semaphore(settings.provider_worker_concurrency)

    async def process_one(work) -> bool:
        async with semaphore:
            return await processor.execute(work)

    results = await asyncio.gather(*(process_one(work) for work in claimed))
    return any(results)


async def run_forever() -> None:
    interval = get_settings().provider_worker_poll_interval_seconds
    while True:
        await run_once()
        await asyncio.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run external HOLD work")
    parser.add_argument("--forever", action="store_true")
    args = parser.parse_args()
    if args.forever:
        asyncio.run(run_forever())
    else:
        print(asyncio.run(run_once()))
