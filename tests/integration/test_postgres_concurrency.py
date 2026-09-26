import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.domain.enums import (
    ReservationLineStatus,
    ReservationStatus,
)
from app.infrastructure.db.models import (
    InternalStockModel,
    ReservationLineModel,
    ReservationModel,
)
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from tests.conftest import (
    api_client,
    create_body,
    create_headers,
    seed_external_source,
    seed_internal_source,
    seed_reservation_line,
)


pytestmark = pytest.mark.postgres


async def test_two_concurrent_api_checkouts_for_last_internal_unit_exactly_one_wins(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory,
        sku="HOT-LAST-UNIT",
        on_hand=1,
    )

    async with api_client(postgres_session_factory) as client:

        async def reserve(user_id: str):
            return await client.post(
                "/reservations",
                headers=create_headers(
                    user_id=user_id,
                    idempotency_key=f"last-unit-{user_id}",
                ),
                json=create_body(source),
            )

        first, second = await asyncio.gather(
            reserve("user-a"),
            reserve("user-b"),
        )

    assert sorted([first.status_code, second.status_code]) == [201, 409]
    loser = first if first.status_code == 409 else second
    assert loser.json()["code"] == "INSUFFICIENT_STOCK"

    async with postgres_session_factory() as session:
        stock = await session.get(InternalStockModel, source.source_id)
        reservations = await session.scalar(select(func.count()).select_from(ReservationModel))
        lines = await session.scalar(select(func.count()).select_from(ReservationLineModel))

    assert stock is not None
    assert stock.on_hand == 1
    assert stock.held == 1
    assert reservations == 1
    assert lines == 1


async def test_skip_locked_claimers_take_distinct_pending_provider_work(
    postgres_session_factory,
):
    first_source = await seed_external_source(
        postgres_session_factory,
        sku="CLAIM-A",
    )
    second_source = await seed_external_source(
        postgres_session_factory,
        sku="CLAIM-B",
        provider_id=first_source.provider_id,
    )
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    first_reservation = await seed_reservation_line(
        postgres_session_factory,
        source=first_source,
        user_id="user-a",
        idempotency_key="claim-a",
        reservation_status=ReservationStatus.RESERVING,
        line_status=ReservationLineStatus.HOLD_PENDING,
        expires_at=expires_at,
    )
    second_reservation = await seed_reservation_line(
        postgres_session_factory,
        source=second_source,
        user_id="user-b",
        idempotency_key="claim-b",
        reservation_status=ReservationStatus.RESERVING,
        line_status=ReservationLineStatus.HOLD_PENDING,
        expires_at=expires_at,
    )

    async def claim_one():
        async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
            claimed = await uow.reservations.claim_pending_external_holds(
                limit=1,
                lease_seconds=30,
            )
            await asyncio.sleep(0.1)
            await uow.commit()
            return claimed

    left, right = await asyncio.gather(claim_one(), claim_one())

    assert len(left) == 1
    assert len(right) == 1
    claimed_ids = {left[0].reservation_id, right[0].reservation_id}
    assert claimed_ids == {first_reservation, second_reservation}

    async with postgres_session_factory() as session:
        lines = (
            await session.scalars(
                select(ReservationLineModel)
                .where(
                    ReservationLineModel.reservation_id.in_([first_reservation, second_reservation])
                )
                .order_by(ReservationLineModel.reservation_id)
            )
        ).all()

    assert len(lines) == 2
    assert all(line.status == ReservationLineStatus.HOLD_IN_PROGRESS for line in lines)
    assert all(line.provider_claim_token is not None for line in lines)
    assert all(line.provider_lease_until is not None for line in lines)


async def test_stale_provider_claim_is_recovered_to_unknown_without_duplicate_transition(
    postgres_session_factory,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="STALE-LEASE",
    )
    claim_token = uuid4()
    reservation_id = await seed_reservation_line(
        postgres_session_factory,
        source=source,
        user_id="user-1",
        idempotency_key="stale-claim",
        reservation_status=ReservationStatus.RESERVING,
        line_status=ReservationLineStatus.HOLD_IN_PROGRESS,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        claim_token=claim_token,
        lease_until=datetime.now(timezone.utc) - timedelta(seconds=10),
    )

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        recovered = await uow.reservations.recover_expired_provider_claims(limit=10)
        await uow.commit()

    assert recovered == 1

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        recovered_again = await uow.reservations.recover_expired_provider_claims(limit=10)
        await uow.commit()
    assert recovered_again == 0

    async with postgres_session_factory() as session:
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )
        reservation = await session.get(ReservationModel, reservation_id)

    assert line is not None
    assert line.status == ReservationLineStatus.HOLD_UNKNOWN
    assert line.provider_claim_token is None
    assert line.provider_lease_until is None
    assert reservation is not None
    assert reservation.status == ReservationStatus.RESERVING


async def test_concurrent_same_create_idempotency_key_holds_stock_once(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory,
        sku="CONCURRENT-CREATE-IDEMPOTENCY",
        on_hand=3,
    )

    async with api_client(postgres_session_factory) as client:

        async def create_once():
            return await client.post(
                "/reservations",
                headers=create_headers(
                    user_id="same-user",
                    idempotency_key="same-concurrent-key",
                ),
                json=create_body(source, quantity=2),
            )

        first, second = await asyncio.gather(create_once(), create_once())

    assert sorted([first.status_code, second.status_code]) == [200, 201]
    assert first.json()["reservation_id"] == second.json()["reservation_id"]

    reservation_id = UUID(first.json()["reservation_id"])
    async with postgres_session_factory() as session:
        stock = await session.get(InternalStockModel, source.source_id)
        reservation_count = await session.scalar(select(func.count()).select_from(ReservationModel))
        line_count = await session.scalar(select(func.count()).select_from(ReservationLineModel))
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )

    assert stock is not None
    assert stock.on_hand == 3
    assert stock.held == 2
    assert reservation_count == 1
    assert line_count == 1
    assert line is not None
    assert line.quantity == 2
    assert line.status == ReservationLineStatus.HELD
