import argparse
import asyncio

from app.bootstrap.config import get_settings
from app.bootstrap.dependencies import get_reconcile_provider_work_service
from app.infrastructure.db.session import AsyncSessionLocal
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork


async def run_once() -> bool:
    settings = get_settings()
    async with SqlAlchemyUnitOfWork(AsyncSessionLocal) as uow:
        # recovers if any worker in the middle of operation crashed before.
        recovered = await uow.reservations.recover_expired_provider_claims(
            limit=settings.provider_worker_batch_size
        )
        holds = await uow.reservations.claim_unknown_external_holds(
            limit=settings.provider_worker_batch_size,
            lease_seconds=settings.provider_worker_lease_seconds,
        )
        releases = await uow.reservations.claim_unknown_external_releases(
            limit=settings.provider_worker_batch_size,
            lease_seconds=settings.provider_worker_lease_seconds,
        )
        await uow.commit()
    if not holds and not releases:
        return bool(recovered)

    service = get_reconcile_provider_work_service()
    semaphore = asyncio.Semaphore(settings.provider_worker_concurrency)

    async def reconcile_hold(work) -> bool:
        async with semaphore:
            return await service.reconcile_hold(work)

    async def reconcile_release(work) -> bool:
        async with semaphore:
            return await service.reconcile_release(work)

    results = await asyncio.gather(
        *(reconcile_hold(work) for work in holds),
        *(reconcile_release(work) for work in releases),
    )
    return bool(recovered) or any(results)


async def run_forever() -> None:
    interval = get_settings().provider_worker_poll_interval_seconds
    while True:
        await run_once()
        await asyncio.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reconcile unknown provider work")
    parser.add_argument("--forever", action="store_true")
    args = parser.parse_args()
    if args.forever:
        asyncio.run(run_forever())
    else:
        print(asyncio.run(run_once()))
