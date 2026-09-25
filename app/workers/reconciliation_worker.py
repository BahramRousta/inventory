import argparse
import asyncio

from app.bootstrap.config import get_settings
from app.bootstrap.dependencies import get_reconcile_provider_work_service


async def run_once() -> bool:
    return await get_reconcile_provider_work_service().execute_one()


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
