import asyncio

from app.bootstrap.config import get_settings
from app.bootstrap.dependencies import get_provider_worker_tick_service


async def run_once() -> str:
    result = await get_provider_worker_tick_service().execute_once()
    return result.value


async def run_forever() -> None:
    """Run the existing single-tick workflow at a configured, bounded cadence."""
    poll_interval = get_settings().provider_worker_poll_interval_seconds
    service = get_provider_worker_tick_service()
    while True:
        await service.execute_once()
        await asyncio.sleep(poll_interval)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run provider workflow work")
    parser.add_argument(
        "--forever",
        action="store_true",
        help="poll continuously; without this flag, run one tick",
    )
    args = parser.parse_args()
    if args.forever:
        asyncio.run(run_forever())
    else:
        print(asyncio.run(run_once()))
