import asyncio
from uuid import UUID

import pytest
from sqlalchemy import select

from app.application.services.cancel.process_releasing_reservation import (
    ProcessReleasingReservationService,
)
from app.domain.enums import ReservationLineStatus, ReservationStatus
from app.infrastructure.db.models import (
    InternalStockModel,
    ReservationLineModel,
    ReservationModel,
)
from tests.conftest import (
    api_client,
    create_body,
    create_headers,
    seed_internal_source,
    uow_factory,
)


pytestmark = pytest.mark.postgres


async def _create_and_cancel_internal(
    postgres_session_factory,
    *,
    sku: str,
    user_id: str,
    idempotency_key: str,
) -> tuple[UUID, UUID]:
    source = await seed_internal_source(
        postgres_session_factory,
        sku=sku,
        on_hand=2,
    )

    async with api_client(postgres_session_factory) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(
                user_id=user_id,
                idempotency_key=idempotency_key,
            ),
            json=create_body(source),
        )
        assert created.status_code == 201
        reservation_id = UUID(created.json()["reservation_id"])

        cancelled = await client.post(
            f"/reservations/{reservation_id}/cancel",
            headers=create_headers(user_id=user_id),
        )
        assert cancelled.status_code == 202

    return reservation_id, source.source_id


async def test_release_preparation_processes_multiple_reservations_in_one_batch(
    postgres_session_factory,
):
    """Scenario: one release-worker iteration has several reservations to prepare.

    Given three internal reservations already moved to RELEASING,
    When ProcessReleasingReservationService executes one bounded batch,
    Then PostgreSQL persists all three reservations as CANCELLED, all lines as
    RELEASED, and restores every held stock unit in that single batch.
    """
    # Given
    created = [
        await _create_and_cancel_internal(
            postgres_session_factory,
            sku=f"RELEASE-BATCH-{index}",
            user_id=f"release-user-{index}",
            idempotency_key=f"release-batch-{index}",
        )
        for index in range(3)
    ]
    reservation_ids = {reservation_id for reservation_id, _ in created}
    source_ids = {source_id for _, source_id in created}

    service = ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    )

    # When
    prepared = await service.execute_batch(limit=10)

    # Then
    assert set(prepared) == reservation_ids

    async with postgres_session_factory() as session:
        reservations = (
            await session.scalars(
                select(ReservationModel).where(
                    ReservationModel.id.in_(reservation_ids)
                )
            )
        ).all()
        lines = (
            await session.scalars(
                select(ReservationLineModel).where(
                    ReservationLineModel.reservation_id.in_(reservation_ids)
                )
            )
        ).all()
        stocks = (
            await session.scalars(
                select(InternalStockModel).where(
                    InternalStockModel.stock_source_id.in_(source_ids)
                )
            )
        ).all()

    assert len(reservations) == 3
    assert all(
        reservation.status == ReservationStatus.CANCELLED
        for reservation in reservations
    )
    assert len(lines) == 3
    assert all(line.status == ReservationLineStatus.RELEASED for line in lines)
    assert len(stocks) == 3
    assert all(stock.on_hand == 2 for stock in stocks)
    assert all(stock.held == 0 for stock in stocks)


async def test_concurrent_release_batches_process_disjoint_reservations(
    postgres_session_factory,
):
    """Scenario: two release workers prepare compensation at the same time.

    Given four independent reservations in RELEASING,
    When two service instances concurrently execute batches of two,
    Then PostgreSQL row locking with SKIP LOCKED gives them disjoint work and
    all four reservations finish compensation exactly once.
    """
    # Given
    created = [
        await _create_and_cancel_internal(
            postgres_session_factory,
            sku=f"RELEASE-CONCURRENT-{index}",
            user_id=f"concurrent-release-user-{index}",
            idempotency_key=f"concurrent-release-{index}",
        )
        for index in range(4)
    ]
    reservation_ids = {reservation_id for reservation_id, _ in created}
    source_ids = {source_id for _, source_id in created}

    first_worker = ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    )
    second_worker = ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    )

    # When
    first_batch, second_batch = await asyncio.gather(
        first_worker.execute_batch(limit=2),
        second_worker.execute_batch(limit=2),
    )

    # Then
    assert len(first_batch) == 2
    assert len(second_batch) == 2
    assert set(first_batch).isdisjoint(second_batch)
    assert set(first_batch) | set(second_batch) == reservation_ids

    async with postgres_session_factory() as session:
        reservations = (
            await session.scalars(
                select(ReservationModel).where(
                    ReservationModel.id.in_(reservation_ids)
                )
            )
        ).all()
        lines = (
            await session.scalars(
                select(ReservationLineModel).where(
                    ReservationLineModel.reservation_id.in_(reservation_ids)
                )
            )
        ).all()
        stocks = (
            await session.scalars(
                select(InternalStockModel).where(
                    InternalStockModel.stock_source_id.in_(source_ids)
                )
            )
        ).all()

    assert len(reservations) == 4
    assert all(
        reservation.status == ReservationStatus.CANCELLED
        for reservation in reservations
    )
    assert len(lines) == 4
    assert all(line.status == ReservationLineStatus.RELEASED for line in lines)
    assert len(stocks) == 4
    assert all(stock.on_hand == 2 for stock in stocks)
    assert all(stock.held == 0 for stock in stocks)
