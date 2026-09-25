import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.enums import ProviderKind
from app.infrastructure.db.models import (
    Base,
    InternalStockModel,
    InventoryProviderModel,
    ProductModel,
    StockSourceModel,
)
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork


@pytest_asyncio.fixture
async def sqlite_session_factory(tmp_path: Path):
    """Fast local database for unit tests only."""
    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def seed_internal_source(sqlite_session_factory):
    product_id = uuid4()
    provider_id = uuid4()
    source_id = uuid4()
    async with sqlite_session_factory.begin() as session:
        session.add(ProductModel(id=product_id, sku="SKU-1", name="Test product"))
        session.add(
            InventoryProviderModel(
                id=provider_id,
                name="InternalStock",
                kind=ProviderKind.INTERNAL,
                enabled=True,
            )
        )
        session.add(
            StockSourceModel(
                id=source_id,
                product_id=product_id,
                provider_id=provider_id,
                provider_sku="SKU-1",
                enabled=True,
            )
        )
        session.add(InternalStockModel(stock_source_id=source_id, on_hand=5, held=0))
    return product_id, source_id


@pytest.fixture
def uow_factory(sqlite_session_factory):
    return lambda: SqlAlchemyUnitOfWork(sqlite_session_factory)


@pytest_asyncio.fixture
async def postgres_session_factory():
    """Disposable real-PostgreSQL schema for E2E/integration tests."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url or not database_url.startswith("postgresql"):
        pytest.skip(
            "Set TEST_DATABASE_URL to a disposable PostgreSQL database "
            "(for example postgresql+psycopg://...)."
        )

    engine = create_async_engine(database_url, pool_size=10, max_overflow=10)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def fake_provider_url():
    """Run the assignment fake provider as an independent HTTP process."""
    repo_root = Path(__file__).resolve().parents[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    env = os.environ.copy()
    env["FAKE_PROVIDER_MODE"] = "success"
    env["FAKE_PROVIDER_TIMEOUT_SECONDS"] = "0.5"

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.fake_provider:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=repo_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"

    try:
        ready = False
        async with httpx.AsyncClient(timeout=0.25) as client:
            for _ in range(60):
                if process.poll() is not None:
                    break
                try:
                    response = await client.get(f"{base_url}/health")
                    if response.status_code == 200:
                        ready = True
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)
        if not ready:
            pytest.fail("Fake provider did not start.")
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
