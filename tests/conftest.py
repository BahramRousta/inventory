from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.enums import ProviderCapability, ProviderKind
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
                capabilities=[ProviderCapability.CHECK.value, ProviderCapability.HOLD.value],
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
