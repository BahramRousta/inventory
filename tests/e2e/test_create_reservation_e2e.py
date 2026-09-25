from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.application.ports.clock import Clock
from app.application.services.create_reservation import CreateReservationService
from app.bootstrap.dependencies import get_create_reservation_service
from app.domain.enums import ProviderCapability, ProviderKind, ReservationLineStatus, ReservationStatus
from app.infrastructure.db.models import (
    InternalStockModel,
    InventoryProviderModel,
    ProductModel,
    ReservationLineModel,
    ReservationModel,
    StockSourceModel,
)
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.main import app


async def test_create_reservation_persists_a_held_internal_line_end_to_end(
    sqlite_session_factory,
):
    """Exercise HTTP -> application -> async SQLAlchemy and verify stored state."""
    product_id, provider_id, source_id = uuid4(), uuid4(), uuid4()
    async with sqlite_session_factory.begin() as session:
        session.add(ProductModel(id=product_id, sku="E2E-SKU-1", name="E2E product"))
        session.add(
            InventoryProviderModel(
                id=provider_id,
                name="E2E Internal Provider",
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
                provider_sku="E2E-SKU-1",
                enabled=True,
            )
        )
        session.add(InternalStockModel(stock_source_id=source_id, on_hand=3, held=0))

    clock = Mock(spec=Clock)
    clock.now.return_value = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)
    service = CreateReservationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(sqlite_session_factory),
        clock=clock,
        ttl_seconds=900,
    )
    app.dependency_overrides[get_create_reservation_service] = lambda: service
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/reservations",
                headers={"Idempotency-Key": "e2e-create-1"},
                json={
                    "user_id": "e2e-user",
                    "items": [
                        {
                            "product_id": str(product_id),
                            "stock_source_id": str(source_id),
                            "quantity": 2,
                        }
                    ],
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    reservation_id = UUID(response.json()["reservation_id"])
    clock.now.assert_called_once_with()

    async with sqlite_session_factory() as session:
        stock = await session.get(InternalStockModel, source_id)
        reservation = await session.get(ReservationModel, reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )

    assert stock is not None
    assert stock.on_hand == 3
    assert stock.held == 2
    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert reservation.user_id == "e2e-user"
    assert line is not None
    assert line.stock_source_id == source_id
    assert line.quantity == 2
    assert line.status == ReservationLineStatus.HELD
