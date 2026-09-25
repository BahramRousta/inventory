import argparse
import asyncio

from app.application.services.expire_reserving_reservation import (
    ExpireReservingReservationService,
)
from app.bootstrap.config import get_settings
from app.infrastructure.db.session import AsyncSessionLocal
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork


async def run_once() -> str | None:
    reservation_id = await ExpireReservingReservationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(AsyncSessionLocal)
    ).execute_one()
    return str(reservation_id) if reservation_id is not None else None


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
