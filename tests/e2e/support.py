from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.application.ports.provider_gateway import (
    ProviderRegistry,
    ProviderRegistryProtocol,
)
from app.application.services.cancel_reservation import CancelReservationService
from app.application.services.confirm_reservation import ConfirmReservationService
from app.application.services.create_reservation import CreateReservationService
from app.application.services.get_reservation import GetReservationService
from app.application.services.process_payment_outcome import ProcessPaymentOutcomeService
from app.bootstrap.dependencies import (
    get_cancel_reservation_service,
    get_confirm_reservation_service,
    get_create_reservation_service,
    get_payment_outcome_service,
    get_reservation_service,
)
from app.infrastructure.clock import SystemClock
from app.infrastructure.db.models import (
    InternalStockModel,
    InventoryProviderModel,
    ProductModel,
    ReservationLineModel,
    ReservationModel,
    StockSourceModel,
)
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.domain.enums import ProviderKind
from app.main import app


@dataclass(frozen=True)
class SeededSource:
    product_id: UUID
    provider_id: UUID
    source_id: UUID


async def seed_internal_source(
    factory,
    *,
    sku: str = "INTERNAL-1",
    on_hand: int = 5,
    source_enabled: bool = True,
    provider_enabled: bool = True,
) -> SeededSource:
    product_id, provider_id, source_id = uuid4(), uuid4(), uuid4()
    async with factory.begin() as session:
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
        session.add(
            InternalStockModel(
                stock_source_id=source_id,
                on_hand=on_hand,
                held=0,
            )
        )
    return SeededSource(product_id, provider_id, source_id)


async def seed_external_source(
    factory,
    *,
    sku: str = "EXTERNAL-1",
    provider_id: UUID | None = None,
    provider_enabled: bool = True,
    source_enabled: bool = True,
) -> SeededSource:
    product_id = uuid4()
    provider_id = provider_id or uuid4()
    source_id = uuid4()
    async with factory.begin() as session:
        session.add(ProductModel(id=product_id, sku=sku, name=f"{sku} product"))
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


def uow_factory(factory):
    return lambda: SqlAlchemyUnitOfWork(factory)


def install_api_overrides(
    factory,
    *,
    providers: ProviderRegistryProtocol | None = None,
    ttl_seconds: int = 900,
) -> None:
    registry = providers or ProviderRegistry()
    make_uow = uow_factory(factory)

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
        uow_factory=make_uow,
    )
    app.dependency_overrides[get_payment_outcome_service] = lambda: ProcessPaymentOutcomeService(
        uow_factory=make_uow,
    )


@asynccontextmanager
async def api_client(
    factory,
    *,
    providers: ProviderRegistryProtocol | None = None,
    ttl_seconds: int = 900,
):
    install_api_overrides(
        factory,
        providers=providers,
        ttl_seconds=ttl_seconds,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def create_headers(
    *,
    user_id: str = "user-1",
    idempotency_key: str | None = None,
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


async def reservation_count(factory) -> int:
    async with factory() as session:
        return int(await session.scalar(select(func.count()).select_from(ReservationModel)) or 0)


async def reservation_line_count(factory) -> int:
    async with factory() as session:
        return int(
            await session.scalar(select(func.count()).select_from(ReservationLineModel)) or 0
        )


def provider_registry(provider_id: UUID, provider) -> ProviderRegistry:
    return ProviderRegistry({provider_id: provider})
