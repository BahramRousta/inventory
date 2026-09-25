import os
import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.dto.reservations import CreateReservationCommand, ReservationItemCommand
from app.application.errors import InsufficientStock
from app.application.services.create_reservation import CreateReservationService
from app.domain.enums import ProviderCapability, ProviderKind
from app.infrastructure.db.models import Base, InternalStockModel, InventoryProviderModel, ProductModel, StockSourceModel
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork


class FixedClock:
    def now(self):
        return datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


@pytest.mark.postgres
async def test_two_concurrent_requests_for_last_unit_exactly_one_wins():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url or not database_url.startswith("postgresql"):
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL database.")

    engine = create_async_engine(database_url, pool_size=5)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    product_id, provider_id, source_id = uuid4(), uuid4(), uuid4()
    async with factory.begin() as session:
        session.add(ProductModel(id=product_id, sku="HOT-1", name="Hot SKU"))
        session.add(InventoryProviderModel(
            id=provider_id,
            name="InternalStock",
            kind=ProviderKind.INTERNAL,
            capabilities=[ProviderCapability.HOLD.value],
            enabled=True,
        ))
        session.add(StockSourceModel(
            id=source_id, product_id=product_id, provider_id=provider_id,
            provider_sku="HOT-1", enabled=True,
        ))
        session.add(InternalStockModel(stock_source_id=source_id, on_hand=1, held=0))

    async def reserve(key: str) -> str:
        service = CreateReservationService(
            uow_factory=lambda: SqlAlchemyUnitOfWork(factory),
            clock=FixedClock(),
            ttl_seconds=900,
        )
        try:
            await service.execute(CreateReservationCommand(
                user_id=key,
                idempotency_key=key,
                items=(ReservationItemCommand(product_id, source_id, 1),),
            ))
            return "success"
        except InsufficientStock:
            return "insufficient"

    results = await asyncio.gather(reserve("a"), reserve("b"))

    assert sorted(results) == ["insufficient", "success"]
    async with factory() as session:
        held = await session.scalar(
            text("select held from internal_stock where stock_source_id = :id"), {"id": source_id}
        )
        assert held == 1
    await engine.dispose()
