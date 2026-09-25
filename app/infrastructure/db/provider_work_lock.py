import hashlib
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class PostgresProviderWorkLock:
    """Session-scoped PostgreSQL advisory lock for a provider operation key."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def try_acquire(self, operation_key: str) -> AsyncIterator[bool]:
        lock_id = _lock_id(operation_key)
        async with self._engine.connect() as connection:
            connection = await connection.execution_options(
                isolation_level="AUTOCOMMIT"
            )
            acquired = bool(
                await connection.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": lock_id},
                )
            )
            try:
                yield acquired
            finally:
                if acquired:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_id)"),
                        {"lock_id": lock_id},
                    )


def _lock_id(operation_key: str) -> int:
    return int.from_bytes(
        hashlib.blake2b(operation_key.encode(), digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )
