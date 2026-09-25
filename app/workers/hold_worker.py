import argparse
import asyncio

from app.application.services.select_pending_provider_hold import (
    SelectPendingProviderHoldService,
)
from app.bootstrap.config import get_settings
from app.bootstrap.dependencies import get_process_pending_provider_hold_service
from app.infrastructure.db.session import AsyncSessionLocal
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork


async def run_once() -> bool:
    work = await SelectPendingProviderHoldService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(AsyncSessionLocal)
    ).execute()
    if work is None:
        return False
    return await get_process_pending_provider_hold_service().execute(work)


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
