import argparse
import asyncio

from app.application.services.expiry.expire_reserving_reservation import (
    ExpireReservingReservationService,
)
from app.bootstrap.config import get_settings
from app.infrastructure.db.session import AsyncSessionLocal
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork


async def run_once() -> int:
    settings = get_settings()
    reservation_ids = await ExpireReservingReservationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(AsyncSessionLocal)
    ).execute_batch(limit=settings.provider_worker_batch_size)
    return len(reservation_ids)


async def run_forever() -> None:
    interval = get_settings().provider_worker_poll_interval_seconds
    while True:
        await run_once()
        await asyncio.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Expire in-progress reservations")
    parser.add_argument("--forever", action="store_true")
    args = parser.parse_args()
    if args.forever:
        asyncio.run(run_forever())
    else:
        print(asyncio.run(run_once()))
