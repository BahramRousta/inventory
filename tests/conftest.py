"""Shared PostgreSQL integration-test fixtures and BDD scenario helpers.

Tests own no database bootstrap code. Every scenario seeds its required
products, providers, sources, and stock through this module, then asserts
persisted rows after crossing the HTTP boundary.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.services.cancel.cancel_reservation import CancelReservationService
from app.application.services.confirm.confirm_reservation import ConfirmReservationService
from app.application.services.create.create_reservation import CreateReservationService
from app.application.services.inquiry.get_reservation import GetReservationService
from app.application.ports.provider_gateway import ProviderRegistry
from app.bootstrap.dependencies import (
    get_cancel_reservation_service,
    get_confirm_reservation_service,
    get_create_reservation_service,
    get_reservation_service,
)
from app.domain.enums import ProviderKind, ReservationLineStatus, ReservationStatus
from app.infrastructure.clock import SystemClock
from app.infrastructure.db.models import (
    Base,
    InternalStockModel,
    InventoryProviderModel,
    ProductModel,
    ReservationLineModel,
    ReservationModel,
    StockSourceModel,
)
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.main import app


@dataclass(frozen=True)
class SeededSource:
    product_id: UUID
    provider_id: UUID
    source_id: UUID


class SeedInventory:
    """Scenario data builder backed by the real disposable PostgreSQL schema."""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def internal(
        self,
        *,
        sku: str = "INTERNAL-1",
        on_hand: int = 5,
        source_enabled: bool = True,
        provider_enabled: bool = True,
    ) -> SeededSource:
        product_id, provider_id, source_id = uuid4(), uuid4(), uuid4()
        async with self._session_factory.begin() as session:
            session.add(ProductModel(id=product_id, sku=sku, name=f"{sku} product"))
            session.add(
                InventoryProviderModel(
                    id=provider_id,
                    name=f"{sku}-provider",
                    kind=ProviderKind.INTERNAL,
                    enabled=provider_enabled,
                )
            )
            session.add(
                StockSourceModel(
                    id=source_id,
                    product_id=product_id,
                    provider_id=provider_id,
                    provider_sku=sku,
                    enabled=source_enabled,
                )
            )
            session.add(InternalStockModel(stock_source_id=source_id, on_hand=on_hand, held=0))
        return SeededSource(product_id, provider_id, source_id)

    async def external(
        self,
        *,
        sku: str = "EXTERNAL-1",
        provider_id: UUID | None = None,
        provider_enabled: bool = True,
        source_enabled: bool = True,
    ) -> SeededSource:
        product_id, source_id = uuid4(), uuid4()
        provider_id = provider_id or uuid4()
        async with self._session_factory.begin() as session:
            session.add(ProductModel(id=product_id, sku=sku, name=f"{sku} product"))
            existing_provider = await session.get(InventoryProviderModel, provider_id)
            if existing_provider is None:
                session.add(
                    InventoryProviderModel(
                        id=provider_id,
                        name=f"{sku}-provider",
                        kind=ProviderKind.EXTERNAL,
                        enabled=provider_enabled,
                    )
                )
            session.add(
                StockSourceModel(
                    id=source_id,
                    product_id=product_id,
                    provider_id=provider_id,
                    provider_sku=sku,
                    enabled=source_enabled,
                )
            )
        return SeededSource(product_id, provider_id, source_id)

    async def product(self, *, sku: str) -> UUID:
        """Seed a catalog product for negative source/product scenarios."""
        product_id = uuid4()
        async with self._session_factory.begin() as session:
            session.add(ProductModel(id=product_id, sku=sku, name=f"{sku} product"))
        return product_id

    async def reservation_line(
        self,
        *,
        source: SeededSource,
        user_id: str,
        idempotency_key: str,
        reservation_status: ReservationStatus,
        line_status: ReservationLineStatus,
        expires_at: datetime,
        quantity: int = 1,
        claim_token: UUID | None = None,
        lease_until: datetime | None = None,
    ) -> UUID:
        """Seed a durable worker-work item for claim/recovery scenarios."""
        reservation_id = uuid4()
        async with self._session_factory.begin() as session:
            session.add(
                ReservationModel(
                    id=reservation_id,
                    user_id=user_id,
                    idempotency_key=idempotency_key,
                    status=reservation_status,
                    expires_at=expires_at,
                )
            )
            session.add(
                ReservationLineModel(
                    reservation_id=reservation_id,
                    stock_source_id=source.source_id,
                    quantity=quantity,
                    status=line_status,
                    provider_claim_token=claim_token,
                    provider_lease_until=lease_until,
                )
            )
        return reservation_id


@pytest_asyncio.fixture
async def postgres_session_factory():
    """A disposable real PostgreSQL schema; never silently substitutes SQLite."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url or not database_url.startswith("postgresql"):
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL database.")

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


@pytest.fixture
def seed_inventory(postgres_session_factory) -> SeedInventory:
    return SeedInventory(postgres_session_factory)


@pytest.fixture
def uow_factory(postgres_session_factory):
    return lambda: SqlAlchemyUnitOfWork(postgres_session_factory)


@pytest.fixture
def request_headers():
    def build(*, user_id: str = "user-1", idempotency_key: str | None = None) -> dict[str, str]:
        headers = {"X-User-Id": user_id}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    return build


@pytest.fixture
def reservation_request():
    def build(source: SeededSource, *, quantity: int = 1) -> dict:
        return {
            "items": [
                {
                    "product_id": str(source.product_id),
                    "stock_source_id": str(source.source_id),
                    "quantity": quantity,
                }
            ]
        }

    return build


@asynccontextmanager
async def api_client(factory, *, ttl_seconds: int = 900):
    """HTTP harness with application dependencies bound to the scenario DB."""
    make_uow = lambda: SqlAlchemyUnitOfWork(factory)
    app.dependency_overrides[get_create_reservation_service] = lambda: CreateReservationService(
        uow_factory=make_uow,
        clock=SystemClock(),
        ttl_seconds=ttl_seconds,
    )
    app.dependency_overrides[get_reservation_service] = lambda: GetReservationService(
        uow_factory=make_uow
    )
    app.dependency_overrides[get_cancel_reservation_service] = lambda: CancelReservationService(
        uow_factory=make_uow
    )
    app.dependency_overrides[get_confirm_reservation_service] = lambda: ConfirmReservationService(
        uow_factory=make_uow
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as http_client:
            yield http_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def api_client_factory(postgres_session_factory):
    """Fixture form for newly written scenarios."""
    return lambda **kwargs: api_client(postgres_session_factory, **kwargs)


async def reservation_count(factory) -> int:
    async with factory() as session:
        return int(await session.scalar(select(func.count()).select_from(ReservationModel)) or 0)


async def reservation_line_count(factory) -> int:
    async with factory() as session:
        return int(
            await session.scalar(select(func.count()).select_from(ReservationLineModel)) or 0
        )


# Temporary callable aliases keep scenario bodies focused on Given/When/Then.
# New scenarios should prefer the ``seed_inventory`` fixture directly.
async def seed_internal_source(factory, **kwargs) -> SeededSource:
    return await SeedInventory(factory).internal(**kwargs)


async def seed_external_source(factory, **kwargs) -> SeededSource:
    return await SeedInventory(factory).external(**kwargs)


async def seed_product(factory, **kwargs) -> UUID:
    return await SeedInventory(factory).product(**kwargs)


async def seed_reservation_line(factory, **kwargs) -> UUID:
    return await SeedInventory(factory).reservation_line(**kwargs)


def provider_registry(provider_id: UUID, provider) -> ProviderRegistry:
    return ProviderRegistry({provider_id: provider})


def create_headers(
    *, user_id: str = "user-1", idempotency_key: str | None = None
) -> dict[str, str]:
    headers = {"X-User-Id": user_id}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def create_body(source: SeededSource, *, quantity: int = 1) -> dict:
    return {
        "items": [
            {
                "product_id": str(source.product_id),
                "stock_source_id": str(source.source_id),
                "quantity": quantity,
            }
        ]
    }
