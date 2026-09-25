import argparse
import asyncio

from app.bootstrap.config import get_settings
from app.bootstrap.dependencies import get_process_releasing_reservation_service
from app.infrastructure.db.session import AsyncSessionLocal
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork


async def run_once() -> bool:
    async with SqlAlchemyUnitOfWork(AsyncSessionLocal) as uow:
        reservation_id = await uow.reservations.get_next_releasing_reservation_id()
    if reservation_id is None:
        return False
    return await get_process_releasing_reservation_service().execute(reservation_id)


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
