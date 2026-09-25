import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.application.dto.reservations import PaymentOutcomeCommand
from app.application.services.expire_reserving_reservation import (
    ExpireReservingReservationService,
)
from app.application.errors import ReservationStateConflict
from app.application.services.process_payment_outcome import ProcessPaymentOutcomeService
from app.domain.enums import (
    PaymentOutcome,
    ReservationLineStatus,
    ReservationStatus,
)
from app.infrastructure.db.models import (
    InternalStockModel,
    OrderModel,
    ReservationLineModel,
    ReservationModel,
)
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from tests.e2e.support import (
    api_client,
    create_body,
    create_headers,
    seed_external_source,
    seed_internal_source,
    uow_factory,
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
        reservations = await session.scalar(
            select(func.count()).select_from(ReservationModel)
        )
        lines = await session.scalar(
            select(func.count()).select_from(ReservationLineModel)
        )

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
    )
    first_reservation = uuid4()
    second_reservation = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

    async with postgres_session_factory.begin() as session:
        session.add_all(
            [
                ReservationModel(
                    id=first_reservation,
                    user_id="user-a",
                    idempotency_key="claim-a",
                    request_fingerprint="a" * 64,
                    status=ReservationStatus.RESERVING,
                    expires_at=expires_at,
                ),
                ReservationModel(
                    id=second_reservation,
                    user_id="user-b",
                    idempotency_key="claim-b",
                    request_fingerprint="b" * 64,
                    status=ReservationStatus.RESERVING,
                    expires_at=expires_at,
                ),
                ReservationLineModel(
                    reservation_id=first_reservation,
                    stock_source_id=first_source.source_id,
                    quantity=1,
                    status=ReservationLineStatus.HOLD_PENDING,
                ),
                ReservationLineModel(
                    reservation_id=second_reservation,
                    stock_source_id=second_source.source_id,
                    quantity=1,
                    status=ReservationLineStatus.HOLD_PENDING,
                ),
            ]
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
                    ReservationLineModel.reservation_id.in_(
                        [first_reservation, second_reservation]
                    )
                )
                .order_by(ReservationLineModel.reservation_id)
            )
        ).all()

    assert len(lines) == 2
    assert all(
        line.status == ReservationLineStatus.HOLD_IN_PROGRESS for line in lines
    )
    assert all(line.provider_claim_token is not None for line in lines)
    assert all(line.provider_lease_until is not None for line in lines)


async def test_stale_provider_claim_is_recovered_to_unknown_without_duplicate_transition(
    postgres_session_factory,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="STALE-LEASE",
    )
    reservation_id = uuid4()
    claim_token = uuid4()

    async with postgres_session_factory.begin() as session:
        session.add(
            ReservationModel(
                id=reservation_id,
                user_id="user-1",
                idempotency_key="stale-claim",
                request_fingerprint="c" * 64,
                status=ReservationStatus.RESERVING,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
        )
        session.add(
            ReservationLineModel(
                reservation_id=reservation_id,
                stock_source_id=source.source_id,
                quantity=1,
                status=ReservationLineStatus.HOLD_IN_PROGRESS,
                provider_claim_token=claim_token,
                provider_lease_until=datetime.now(timezone.utc)
                - timedelta(seconds=10),
            )
        )

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        recovered = await uow.reservations.recover_expired_provider_claims(
            limit=10
        )
        await uow.commit()

    assert recovered == 1

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        recovered_again = await uow.reservations.recover_expired_provider_claims(
            limit=10
        )
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


async def test_payment_and_expiry_concurrency_has_one_local_transition_winner(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory,
        sku="PAYMENT-EXPIRY-RACE",
        on_hand=1,
    )

    async with api_client(postgres_session_factory) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(
                user_id="race-user",
                idempotency_key="race-create",
            ),
            json=create_body(source),
        )
    assert created.status_code == 201
    reservation_id = UUID(created.json()["reservation_id"])

    payment_service = ProcessPaymentOutcomeService(
        uow_factory=uow_factory(postgres_session_factory)
    )
    expiry_service = ExpireReservingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    )

    # Both operations start concurrently while the reservation is still valid.
    # The payment transition can claim ACTIVE -> CONFIRMING; expiry can only
    # claim when database time reaches expires_at. Exactly one terminal local
    # result is permitted.
    payment_task = asyncio.create_task(
        payment_service.execute(
            PaymentOutcomeCommand(
                event_id=uuid4(),
                reservation_id=reservation_id,
                user_id="race-user",
                outcome=PaymentOutcome.SUCCESS,
            )
        )
    )
    expiry_task = asyncio.create_task(expiry_service.execute_batch(limit=10))
    payment_result, expired_ids = await asyncio.gather(
        payment_task,
        expiry_task,
    )

    assert payment_result.status == ReservationStatus.CONFIRMED
    assert reservation_id not in expired_ids

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        stock = await session.get(InternalStockModel, source.source_id)
        order_count = await session.scalar(
            select(func.count()).select_from(OrderModel)
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.CONFIRMED
    assert stock is not None
    assert stock.on_hand == 0
    assert stock.held == 0
    assert order_count == 1


async def test_concurrent_duplicate_payment_event_is_harmless_and_creates_one_order(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory,
        sku="CONCURRENT-PAYMENT-DUP",
        on_hand=1,
    )

    async with api_client(postgres_session_factory) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(
                user_id="dup-race-user",
                idempotency_key="dup-race-create",
            ),
            json=create_body(source),
        )
    assert created.status_code == 201
    reservation_id = UUID(created.json()["reservation_id"])

    event_id = uuid4()
    command = PaymentOutcomeCommand(
        event_id=event_id,
        reservation_id=reservation_id,
        user_id="dup-race-user",
        outcome=PaymentOutcome.SUCCESS,
    )

    async def deliver():
        return await ProcessPaymentOutcomeService(
            uow_factory=uow_factory(postgres_session_factory)
        ).execute(command)

    first, second = await asyncio.gather(deliver(), deliver())

    assert first.status == ReservationStatus.CONFIRMED
    assert second.status == ReservationStatus.CONFIRMED
    assert first.order_id == second.order_id

    from app.infrastructure.db.models import PaymentEventModel

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        stock = await session.get(InternalStockModel, source.source_id)
        order_count = await session.scalar(
            select(func.count()).select_from(OrderModel)
        )
        event_count = await session.scalar(
            select(func.count()).select_from(PaymentEventModel)
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.CONFIRMED
    assert stock is not None
    assert stock.on_hand == 0
    assert stock.held == 0
    assert order_count == 1
    assert event_count == 1


async def test_expiry_wins_when_payment_success_arrives_after_database_ttl(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory,
        sku="EXPIRY-WINS-RACE",
        on_hand=1,
    )

    async with api_client(postgres_session_factory) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(
                user_id="expiry-race-user",
                idempotency_key="expiry-race-create",
            ),
            json=create_body(source),
        )
    assert created.status_code == 201
    reservation_id = UUID(created.json()["reservation_id"])

    from sqlalchemy import update

    async with postgres_session_factory.begin() as session:
        await session.execute(
            update(ReservationModel)
            .where(ReservationModel.id == reservation_id)
            .values(
                expires_at=datetime.now(timezone.utc) - timedelta(milliseconds=1)
            )
        )

    payment_service = ProcessPaymentOutcomeService(
        uow_factory=uow_factory(postgres_session_factory)
    )
    expiry_service = ExpireReservingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    )

    async def pay():
        try:
            await payment_service.execute(
                PaymentOutcomeCommand(
                    event_id=uuid4(),
                    reservation_id=reservation_id,
                    user_id="expiry-race-user",
                    outcome=PaymentOutcome.SUCCESS,
                )
            )
            return "confirmed"
        except ReservationStateConflict:
            return "rejected"

    payment_result, expired_ids = await asyncio.gather(
        pay(),
        expiry_service.execute_batch(limit=10),
    )

    assert payment_result == "rejected"
    assert reservation_id in expired_ids

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        stock = await session.get(InternalStockModel, source.source_id)
        order_count = await session.scalar(
            select(func.count()).select_from(OrderModel)
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.RELEASING
    assert reservation.release_reason == "EXPIRED"
    assert stock is not None
    assert stock.on_hand == 1
    assert stock.held == 1
    assert order_count == 0


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
        reservation_count = await session.scalar(
            select(func.count()).select_from(ReservationModel)
        )
        line_count = await session.scalar(
            select(func.count()).select_from(ReservationLineModel)
        )
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
