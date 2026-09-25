from datetime import datetime, timezone

import pytest

from app.application.dto.reservations import CreateReservationCommand, ReservationItemCommand
from app.application.errors import IdempotencyConflict, InsufficientStock
from app.application.services.create_reservation import CreateReservationService
from app.domain.enums import ReservationStatus
from app.infrastructure.db.models import InternalStockModel


class FixedClock:
    def now(self):
        return datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


def make_service(uow_factory):
    return CreateReservationService(uow_factory=uow_factory, clock=FixedClock(), ttl_seconds=900)


async def test_create_internal_reservation_holds_stock(
    uow_factory, sqlite_session_factory, seed_internal_source
):
    product_id, source_id = seed_internal_source
    service = make_service(uow_factory)

    result = await service.execute(
        CreateReservationCommand(
            user_id="user-1",
            idempotency_key="checkout-1",
            items=(ReservationItemCommand(product_id, source_id, 2),),
        )
    )

    assert result.status == ReservationStatus.ACTIVE
    assert result.payment_allowed is True
    async with sqlite_session_factory() as session:
        stock = await session.get(InternalStockModel, source_id)
        assert stock.on_hand == 5
        assert stock.held == 2


async def test_idempotent_replay_does_not_hold_twice(
    uow_factory, sqlite_session_factory, seed_internal_source
):
    product_id, source_id = seed_internal_source
    service = make_service(uow_factory)
    command = CreateReservationCommand(
        user_id="user-1",
        idempotency_key="checkout-1",
        items=(ReservationItemCommand(product_id, source_id, 2),),
    )

    first = await service.execute(command)
    second = await service.execute(command)

    assert first.reservation_id == second.reservation_id
    async with sqlite_session_factory() as session:
        stock = await session.get(InternalStockModel, source_id)
        assert stock.held == 2


async def test_idempotency_key_with_different_body_conflicts(uow_factory, seed_internal_source):
    product_id, source_id = seed_internal_source
    service = make_service(uow_factory)
    await service.execute(
        CreateReservationCommand(
            user_id="user-1",
            idempotency_key="same-key",
            items=(ReservationItemCommand(product_id, source_id, 1),),
        )
    )

    with pytest.raises(IdempotencyConflict):
        await service.execute(
            CreateReservationCommand(
                user_id="user-1",
                idempotency_key="same-key",
                items=(ReservationItemCommand(product_id, source_id, 2),),
            )
        )


async def test_insufficient_stock_rolls_back_whole_reservation(
    uow_factory, sqlite_session_factory, seed_internal_source
):
    product_id, source_id = seed_internal_source
    service = make_service(uow_factory)

    with pytest.raises(InsufficientStock):
        await service.execute(
            CreateReservationCommand(
                user_id="user-1",
                idempotency_key="too-many",
                items=(ReservationItemCommand(product_id, source_id, 6),),
            )
        )

    async with sqlite_session_factory() as session:
        stock = await session.get(InternalStockModel, source_id)
        assert stock.held == 0
