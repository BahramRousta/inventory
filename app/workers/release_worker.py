import argparse
import asyncio

from app.bootstrap.config import get_settings
from app.bootstrap.dependencies import (
    get_process_claimed_provider_release_service,
    get_process_releasing_reservation_service,
)
from app.infrastructure.db.session import AsyncSessionLocal
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork


async def run_once() -> bool:
    settings = get_settings()

    prepared = await get_process_releasing_reservation_service().execute_batch(
        limit=settings.provider_worker_batch_size
    )

    # External provider calls remain a separate phase. Pending release lines are
    # durably claimed with leases before network I/O, so no DB transaction is
    # held while a provider is slow or unavailable.
    async with SqlAlchemyUnitOfWork(AsyncSessionLocal) as uow:
        claimed = await uow.reservations.claim_pending_external_releases(
            limit=settings.provider_worker_batch_size,
            lease_seconds=settings.provider_worker_lease_seconds,
        )
        await uow.commit()

    if not claimed:
        return bool(prepared)

    processor = get_process_claimed_provider_release_service()
    semaphore = asyncio.Semaphore(settings.provider_worker_concurrency)

    async def process_one(work) -> bool:
        async with semaphore:
            return await processor.execute(work)

    results = await asyncio.gather(*(process_one(work) for work in claimed))
    return bool(prepared) or any(results)


async def run_forever() -> None:
    interval = get_settings().provider_worker_poll_interval_seconds
    while True:
        await run_once()
        await asyncio.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run external RELEASE work")
    parser.add_argument("--forever", action="store_true")
    args = parser.parse_args()
    if args.forever:
        asyncio.run(run_forever())
    else:
        print(asyncio.run(run_once()))
