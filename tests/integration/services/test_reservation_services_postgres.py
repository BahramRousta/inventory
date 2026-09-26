from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.application.dto.reservations import (
    CreateReservationCommand,
    ReservationItemCommand,
)
from app.application.ports.provider_gateway import (
    ProviderRegistry,
    ProviderReleaseOutcome,
    ProviderReservationLookupOutcome,
    ProviderReserveOutcome,
)
from app.application.services.cancel.cancel_reservation import (
    CancelReservationService,
)
from app.application.services.cancel.process_claimed_provider_release import (
    ProcessClaimedProviderReleaseService,
)
from app.application.services.cancel.process_releasing_reservation import (
    ProcessReleasingReservationService,
)
from app.application.services.confirm.confirm_reservation import (
    ConfirmReservationService,
)
from app.application.services.create.claim_pending_provider_holds import (
    ClaimPendingProviderHoldsService,
)
from app.application.services.create.create_reservation import (
    CreateReservationService,
)
from app.application.services.create.process_pending_provider_hold import (
    ProcessPendingProviderHoldService,
)
from app.application.services.expiry.expire_reserving_reservation import (
    ExpireReservingReservationService,
)
from app.application.services.inquiry.get_reservation import (
    GetReservationService,
)
from app.application.services.reconciliation.reconcile_provider_work import (
    ReconcileProviderWorkService,
)
from app.domain.enums import ReservationLineStatus, ReservationStatus
from app.infrastructure.clock import SystemClock
from app.infrastructure.db.models import (
    InternalStockModel,
    OrderModel,
    ReservationLineModel,
    ReservationModel,
)
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.infrastructure.providers.mock import (
    MockAvailabilityProvider,
    MockReservationProvider,
)
from tests.e2e.support import (
    SeededSource,
    seed_external_source,
    seed_internal_source,
    uow_factory,
)


pytestmark = pytest.mark.postgres


def _create_command(
    source: SeededSource,
    *,
    user_id: str,
    idempotency_key: str,
    quantity: int = 1,
) -> CreateReservationCommand:
    return CreateReservationCommand(
        user_id=user_id,
        idempotency_key=idempotency_key,
        items=(
            ReservationItemCommand(
                product_id=source.product_id,
                stock_source_id=source.source_id,
                quantity=quantity,
            ),
        ),
    )


async def _reservation_line(factory, reservation_id: UUID) -> ReservationLineModel:
    async with factory() as session:
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )
        assert line is not None
        return line


async def _create_external_and_claim(
    factory,
    source: SeededSource,
    *,
    key: str,
):
    create_service = CreateReservationService(
        uow_factory=uow_factory(factory),
        clock=SystemClock(),
        ttl_seconds=900,
    )
    created = await create_service.execute(
        _create_command(
            source,
            user_id="service-user",
            idempotency_key=key,
        )
    )

    claim_service = ClaimPendingProviderHoldsService(
        uow_factory=uow_factory(factory),
        batch_size=10,
        lease_seconds=60,
    )
    claimed = await claim_service.execute()
    return created.reservation_id, claimed[0]


async def _create_and_hold_external(
    factory,
    source: SeededSource,
    provider: MockReservationProvider | MockAvailabilityProvider,
    *,
    key: str,
):
    reservation_id, work = await _create_external_and_claim(
        factory,
        source,
        key=key,
    )

    processor = ProcessPendingProviderHoldService(
        uow_factory=uow_factory(factory),
        providers=ProviderRegistry({source.provider_id: provider}),
    )
    await processor.execute(work)
    return reservation_id


async def test_create_internal_reservation_persists_active_hold(
    postgres_session_factory,
):
    """Scenario: create an internal reservation when enough stock exists.

    Given an enabled internal stock source with five units on hand,
    When CreateReservationService reserves two units,
    Then PostgreSQL stores one ACTIVE reservation, one HELD line, and two held
    inventory units without changing on-hand quantity.
    """
    # Given
    source = await seed_internal_source(
        postgres_session_factory,
        sku="SERVICE-CREATE-INTERNAL",
        on_hand=5,
    )
    service = CreateReservationService(
        uow_factory=uow_factory(postgres_session_factory),
        clock=SystemClock(),
        ttl_seconds=900,
    )

    # When
    result = await service.execute(
        _create_command(
            source,
            user_id="create-user",
            idempotency_key="service-create-internal",
            quantity=2,
        )
    )

    # Then
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, result.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == result.reservation_id
            )
        )
        stock = await session.get(InternalStockModel, source.source_id)
        reservation_count = await session.scalar(
            select(func.count()).select_from(ReservationModel)
        )
        line_count = await session.scalar(
            select(func.count()).select_from(ReservationLineModel)
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert line is not None
    assert line.status == ReservationLineStatus.HELD
    assert line.quantity == 2
    assert stock is not None
    assert stock.on_hand == 5
    assert stock.held == 2
    assert reservation_count == 1
    assert line_count == 1


async def test_create_internal_reservation_replay_does_not_hold_stock_twice(
    postgres_session_factory,
):
    """Scenario: retry the same create command with the same idempotency key.

    Given a previously persisted internal reservation,
    When CreateReservationService receives the same command again,
    Then PostgreSQL still contains one reservation and one line and the held
    quantity is not incremented a second time.
    """
    # Given
    source = await seed_internal_source(
        postgres_session_factory,
        sku="SERVICE-CREATE-IDEMPOTENT",
        on_hand=4,
    )
    service = CreateReservationService(
        uow_factory=uow_factory(postgres_session_factory),
        clock=SystemClock(),
        ttl_seconds=900,
    )
    command = _create_command(
        source,
        user_id="idempotent-user",
        idempotency_key="service-idempotent-key",
        quantity=2,
    )
    first = await service.execute(command)

    # When
    await service.execute(command)

    # Then
    async with postgres_session_factory() as session:
        stock = await session.get(InternalStockModel, source.source_id)
        reservations = await session.scalar(
            select(func.count()).select_from(ReservationModel)
        )
        lines = await session.scalar(
            select(func.count()).select_from(ReservationLineModel)
        )
        persisted = await session.get(ReservationModel, first.reservation_id)

    assert persisted is not None
    assert persisted.status == ReservationStatus.ACTIVE
    assert stock is not None
    assert stock.on_hand == 4
    assert stock.held == 2
    assert reservations == 1
    assert lines == 1


async def test_create_external_reservation_persists_pending_provider_work(
    postgres_session_factory,
):
    """Scenario: create a reservation for an enabled external stock source.

    Given an enabled external provider and source,
    When CreateReservationService accepts the reservation,
    Then PostgreSQL stores the reservation as RESERVING and the line as
    HOLD_PENDING without requiring a provider call inside the create transaction.
    """
    # Given
    source = await seed_external_source(
        postgres_session_factory,
        sku="SERVICE-CREATE-EXTERNAL",
    )
    service = CreateReservationService(
        uow_factory=uow_factory(postgres_session_factory),
        clock=SystemClock(),
        ttl_seconds=900,
    )

    # When
    result = await service.execute(
        _create_command(
            source,
            user_id="external-user",
            idempotency_key="service-create-external",
        )
    )

    # Then
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, result.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == result.reservation_id
            )
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.RESERVING
    assert line is not None
    assert line.status == ReservationLineStatus.HOLD_PENDING
    assert line.external_hold_ref is None
    assert line.provider_claim_token is None
    assert line.provider_lease_until is None


async def test_claim_pending_provider_hold_persists_claim_token_and_lease(
    postgres_session_factory,
):
    """Scenario: claim durable external reservation work.

    Given one external reservation line in HOLD_PENDING,
    When ClaimPendingProviderHoldsService claims a batch,
    Then PostgreSQL moves the line to HOLD_IN_PROGRESS and persists both a
    claim token and lease deadline.
    """
    # Given
    source = await seed_external_source(
        postgres_session_factory,
        sku="SERVICE-CLAIM-HOLD",
    )
    create_service = CreateReservationService(
        uow_factory=uow_factory(postgres_session_factory),
        clock=SystemClock(),
        ttl_seconds=900,
    )
    created = await create_service.execute(
        _create_command(
            source,
            user_id="claim-user",
            idempotency_key="service-claim-hold",
        )
    )
    service = ClaimPendingProviderHoldsService(
        uow_factory=uow_factory(postgres_session_factory),
        batch_size=10,
        lease_seconds=60,
    )

    # When
    await service.execute()

    # Then
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, created.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == created.reservation_id
            )
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.RESERVING
    assert line is not None
    assert line.status == ReservationLineStatus.HOLD_IN_PROGRESS
    assert line.provider_claim_token is not None
    assert line.provider_lease_until is not None


async def test_process_pending_provider_reservation_success_activates_reservation(
    postgres_session_factory,
):
    """Scenario: an external reservation provider accepts reserve().

    Given a claimed external line and a mocked provider returning RESERVED,
    When ProcessPendingProviderHoldService executes the claim,
    Then PostgreSQL stores the external reference, clears the claim ownership,
    marks the line HELD, and activates the reservation.
    """
    # Given
    source = await seed_external_source(
        postgres_session_factory,
        sku="SERVICE-PROVIDER-SUCCESS",
    )
    reservation_id, work = await _create_external_and_claim(
        postgres_session_factory,
        source,
        key="service-provider-success",
    )
    provider = MockReservationProvider()
    service = ProcessPendingProviderHoldService(
        uow_factory=uow_factory(postgres_session_factory),
        providers=ProviderRegistry({source.provider_id: provider}),
    )

    # When
    await service.execute(work)

    # Then
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert line is not None
    assert line.status == ReservationLineStatus.HELD
    assert line.external_hold_ref is not None
    assert line.provider_claim_token is None
    assert line.provider_lease_until is None
    assert line.held_at is not None


async def test_process_query_provider_reservation_success_activates_reservation(
    postgres_session_factory,
):
    """Scenario: a query-style provider accepts reserve() through availability.

    Given a claimed external line and a mocked query provider with enough stock,
    When ProcessPendingProviderHoldService executes the common reserve contract,
    Then PostgreSQL persists the line as HELD and the reservation as ACTIVE,
    proving the service does not branch on provider implementation type.
    """
    # Given
    source = await seed_external_source(
        postgres_session_factory,
        sku="SERVICE-QUERY-PROVIDER",
    )
    reservation_id, work = await _create_external_and_claim(
        postgres_session_factory,
        source,
        key="service-query-provider",
    )
    provider = MockAvailabilityProvider(available_quantity=10)
    service = ProcessPendingProviderHoldService(
        uow_factory=uow_factory(postgres_session_factory),
        providers=ProviderRegistry({source.provider_id: provider}),
    )

    # When
    await service.execute(work)

    # Then
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert line is not None
    assert line.status == ReservationLineStatus.HELD
    assert line.external_hold_ref is not None
    assert line.provider_claim_token is None
    assert line.provider_lease_until is None


async def test_process_pending_provider_decline_moves_reservation_to_releasing(
    postgres_session_factory,
):
    """Scenario: an external provider definitively declines a reservation.

    Given a claimed external line and a mocked provider returning DECLINED,
    When ProcessPendingProviderHoldService persists the result,
    Then PostgreSQL marks the line FAILED and moves the reservation to
    RELEASING with CREATE_FAILED as its release reason.
    """
    # Given
    source = await seed_external_source(
        postgres_session_factory,
        sku="SERVICE-PROVIDER-DECLINE",
    )
    reservation_id, work = await _create_external_and_claim(
        postgres_session_factory,
        source,
        key="service-provider-decline",
    )
    provider = MockReservationProvider(
        reserve_outcome=ProviderReserveOutcome.DECLINED,
    )
    service = ProcessPendingProviderHoldService(
        uow_factory=uow_factory(postgres_session_factory),
        providers=ProviderRegistry({source.provider_id: provider}),
    )

    # When
    await service.execute(work)

    # Then
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.RELEASING
    assert reservation.release_reason == "CREATE_FAILED"
    assert line is not None
    assert line.status == ReservationLineStatus.FAILED
    assert line.provider_claim_token is None
    assert line.provider_lease_until is None


async def test_process_pending_provider_unknown_persists_reconciliation_state(
    postgres_session_factory,
):
    """Scenario: the external reservation outcome is ambiguous.

    Given a claimed external line and a mocked provider returning UNKNOWN,
    When ProcessPendingProviderHoldService handles the result,
    Then PostgreSQL keeps the reservation non-active and stores HOLD_UNKNOWN so
    reconciliation can determine the remote truth later.
    """
    # Given
    source = await seed_external_source(
        postgres_session_factory,
        sku="SERVICE-PROVIDER-UNKNOWN",
    )
    reservation_id, work = await _create_external_and_claim(
        postgres_session_factory,
        source,
        key="service-provider-unknown",
    )
    provider = MockReservationProvider(
        reserve_outcome=ProviderReserveOutcome.UNKNOWN,
    )
    service = ProcessPendingProviderHoldService(
        uow_factory=uow_factory(postgres_session_factory),
        providers=ProviderRegistry({source.provider_id: provider}),
    )

    # When
    await service.execute(work)

    # Then
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.RESERVING
    assert line is not None
    assert line.status == ReservationLineStatus.HOLD_UNKNOWN
    assert line.provider_claim_token is None
    assert line.provider_lease_until is None


async def test_get_reservation_service_does_not_mutate_persisted_state(
    postgres_session_factory,
):
    """Scenario: read an existing reservation through the inquiry service.

    Given an ACTIVE internal reservation with a HELD line,
    When GetReservationService loads the reservation,
    Then PostgreSQL retains the same reservation, line, and inventory values.
    """
    # Given
    source = await seed_internal_source(
        postgres_session_factory,
        sku="SERVICE-INQUIRY",
        on_hand=3,
    )
    create_service = CreateReservationService(
        uow_factory=uow_factory(postgres_session_factory),
        clock=SystemClock(),
        ttl_seconds=900,
    )
    created = await create_service.execute(
        _create_command(
            source,
            user_id="inquiry-user",
            idempotency_key="service-inquiry",
            quantity=2,
        )
    )
    service = GetReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    )

    # When
    await service.execute(
        created.reservation_id,
        user_id="inquiry-user",
    )

    # Then
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, created.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == created.reservation_id
            )
        )
        stock = await session.get(InternalStockModel, source.source_id)

    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert line is not None
    assert line.status == ReservationLineStatus.HELD
    assert line.quantity == 2
    assert stock is not None
    assert stock.on_hand == 3
    assert stock.held == 2


async def test_confirm_internal_reservation_consumes_hold_and_creates_order(
    postgres_session_factory,
):
    """Scenario: confirm an active internal reservation.

    Given an ACTIVE reservation with two internal units HELD,
    When ConfirmReservationService executes,
    Then PostgreSQL consumes the held stock, confirms the line and reservation,
    and persists exactly one final order.
    """
    # Given
    source = await seed_internal_source(
        postgres_session_factory,
        sku="SERVICE-CONFIRM",
        on_hand=5,
    )
    create_service = CreateReservationService(
        uow_factory=uow_factory(postgres_session_factory),
        clock=SystemClock(),
        ttl_seconds=900,
    )
    created = await create_service.execute(
        _create_command(
            source,
            user_id="confirm-user",
            idempotency_key="service-confirm",
            quantity=2,
        )
    )
    service = ConfirmReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    )

    # When
    await service.execute(
        created.reservation_id,
        user_id="confirm-user",
    )

    # Then
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, created.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == created.reservation_id
            )
        )
        stock = await session.get(InternalStockModel, source.source_id)
        order = await session.scalar(
            select(OrderModel).where(
                OrderModel.reservation_id == created.reservation_id
            )
        )
        order_count = await session.scalar(
            select(func.count()).select_from(OrderModel)
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.CONFIRMED
    assert reservation.confirmed_at is not None
    assert line is not None
    assert line.status == ReservationLineStatus.CONFIRMED
    assert line.committed_at is not None
    assert stock is not None
    assert stock.on_hand == 3
    assert stock.held == 0
    assert order is not None
    assert order.user_id == "confirm-user"
    assert order_count == 1


async def test_confirm_replay_keeps_single_order_and_single_inventory_consumption(
    postgres_session_factory,
):
    """Scenario: confirm the same reservation more than once.

    Given an already CONFIRMED internal reservation,
    When ConfirmReservationService receives a repeated confirmation,
    Then PostgreSQL still contains one order and inventory has been consumed
    exactly once.
    """
    # Given
    source = await seed_internal_source(
        postgres_session_factory,
        sku="SERVICE-CONFIRM-IDEMPOTENT",
        on_hand=3,
    )
    create_service = CreateReservationService(
        uow_factory=uow_factory(postgres_session_factory),
        clock=SystemClock(),
        ttl_seconds=900,
    )
    created = await create_service.execute(
        _create_command(
            source,
            user_id="confirm-repeat-user",
            idempotency_key="service-confirm-repeat",
            quantity=1,
        )
    )
    service = ConfirmReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    )
    await service.execute(
        created.reservation_id,
        user_id="confirm-repeat-user",
    )

    # When
    await service.execute(
        created.reservation_id,
        user_id="confirm-repeat-user",
    )

    # Then
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, created.reservation_id)
        stock = await session.get(InternalStockModel, source.source_id)
        order_count = await session.scalar(
            select(func.count())
            .select_from(OrderModel)
            .where(OrderModel.reservation_id == created.reservation_id)
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.CONFIRMED
    assert stock is not None
    assert stock.on_hand == 2
    assert stock.held == 0
    assert order_count == 1


async def test_cancel_then_release_internal_reservation_restores_availability(
    postgres_session_factory,
):
    """Scenario: cancel an active internal reservation.

    Given an ACTIVE internal reservation with held stock,
    When CancelReservationService starts release and
    ProcessReleasingReservationService executes compensation,
    Then PostgreSQL marks the reservation CANCELLED, the line RELEASED, and
    restores held quantity without reducing on-hand inventory.
    """
    # Given
    source = await seed_internal_source(
        postgres_session_factory,
        sku="SERVICE-CANCEL",
        on_hand=4,
    )
    create_service = CreateReservationService(
        uow_factory=uow_factory(postgres_session_factory),
        clock=SystemClock(),
        ttl_seconds=900,
    )
    created = await create_service.execute(
        _create_command(
            source,
            user_id="cancel-user",
            idempotency_key="service-cancel",
            quantity=2,
        )
    )
    cancel_service = CancelReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    )
    release_service = ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    )

    # When
    await cancel_service.execute(
        created.reservation_id,
        user_id="cancel-user",
    )
    await release_service.execute(created.reservation_id)

    # Then
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, created.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == created.reservation_id
            )
        )
        stock = await session.get(InternalStockModel, source.source_id)
        order_count = await session.scalar(
            select(func.count()).select_from(OrderModel)
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED
    assert reservation.release_reason == "USER_CANCELLED"
    assert line is not None
    assert line.status == ReservationLineStatus.RELEASED
    assert line.released_at is not None
    assert stock is not None
    assert stock.on_hand == 4
    assert stock.held == 0
    assert order_count == 0


async def test_expiry_service_moves_expired_reservation_through_release_to_expired(
    postgres_session_factory,
):
    """Scenario: an active reservation passes its TTL without confirmation.

    Given an internal reservation whose expires_at is already in the past,
    When ExpireReservingReservationService claims it and the release service
    compensates the held inventory,
    Then PostgreSQL ends in EXPIRED with the line RELEASED and stock available.
    """
    # Given
    source = await seed_internal_source(
        postgres_session_factory,
        sku="SERVICE-EXPIRY",
        on_hand=2,
    )
    create_service = CreateReservationService(
        uow_factory=uow_factory(postgres_session_factory),
        clock=SystemClock(),
        ttl_seconds=-1,
    )
    created = await create_service.execute(
        _create_command(
            source,
            user_id="expiry-user",
            idempotency_key="service-expiry",
        )
    )
    expiry_service = ExpireReservingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    )
    release_service = ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    )

    # When
    await expiry_service.execute_batch(limit=10)
    await release_service.execute(created.reservation_id)

    # Then
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, created.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == created.reservation_id
            )
        )
        stock = await session.get(InternalStockModel, source.source_id)

    assert reservation is not None
    assert reservation.status == ReservationStatus.EXPIRED
    assert reservation.release_reason == "EXPIRED"
    assert reservation.expires_at <= datetime.now(timezone.utc)
    assert line is not None
    assert line.status == ReservationLineStatus.RELEASED
    assert stock is not None
    assert stock.on_hand == 2
    assert stock.held == 0


async def test_external_cancel_and_provider_release_persists_cancelled_state(
    postgres_session_factory,
):
    """Scenario: cancel an external reservation whose provider release succeeds.

    Given an ACTIVE external reservation with a durable provider reference,
    When cancellation prepares RELEASE_PENDING and
    ProcessClaimedProviderReleaseService receives a mocked RELEASED result,
    Then PostgreSQL marks the line RELEASED and the reservation CANCELLED.
    """
    # Given
    source = await seed_external_source(
        postgres_session_factory,
        sku="SERVICE-EXTERNAL-RELEASE",
    )
    provider = MockReservationProvider()
    reservation_id = await _create_and_hold_external(
        postgres_session_factory,
        source,
        provider,
        key="service-external-release",
    )
    cancel_service = CancelReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    )
    prepare_release_service = ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    )
    await cancel_service.execute(
        reservation_id,
        user_id="service-user",
    )
    await prepare_release_service.execute(reservation_id)

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        claimed = await uow.reservations.claim_pending_external_releases(
            limit=10,
            lease_seconds=60,
        )
        await uow.commit()

    release_service = ProcessClaimedProviderReleaseService(
        uow_factory=uow_factory(postgres_session_factory),
        providers=ProviderRegistry({source.provider_id: provider}),
    )

    # When
    await release_service.execute(claimed[0])

    # Then
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED
    assert reservation.release_reason == "USER_CANCELLED"
    assert line is not None
    assert line.status == ReservationLineStatus.RELEASED
    assert line.released_at is not None
    assert line.provider_claim_token is None
    assert line.provider_lease_until is None


async def test_reconcile_unknown_reservation_to_held_and_active(
    postgres_session_factory,
):
    """Scenario: reconciliation discovers that an ambiguous reserve succeeded.

    Given an external line persisted as HOLD_UNKNOWN,
    When ReconcileProviderWorkService receives a mocked RESERVED lookup,
    Then PostgreSQL moves the line to HELD and the reservation to ACTIVE.
    """
    # Given
    source = await seed_external_source(
        postgres_session_factory,
        sku="SERVICE-RECONCILE-HOLD",
    )
    provider = MockReservationProvider(
        reserve_outcome=ProviderReserveOutcome.UNKNOWN,
        lookup_outcome=ProviderReservationLookupOutcome.UNKNOWN,
    )
    reservation_id, work = await _create_external_and_claim(
        postgres_session_factory,
        source,
        key="service-reconcile-hold",
    )
    reserve_service = ProcessPendingProviderHoldService(
        uow_factory=uow_factory(postgres_session_factory),
        providers=ProviderRegistry({source.provider_id: provider}),
    )
    await reserve_service.execute(work)

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        unknown = await uow.reservations.claim_unknown_external_holds(
            limit=10,
            lease_seconds=60,
        )
        await uow.commit()

    provider.lookup_outcome = ProviderReservationLookupOutcome.RESERVED
    reconcile_service = ReconcileProviderWorkService(
        uow_factory=uow_factory(postgres_session_factory),
        providers=ProviderRegistry({source.provider_id: provider}),
    )

    # When
    await reconcile_service.reconcile_hold(unknown[0])

    # Then
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert line is not None
    assert line.status == ReservationLineStatus.HELD
    assert line.external_hold_ref is not None
    assert line.provider_claim_token is None
    assert line.provider_lease_until is None


async def test_reconcile_unknown_release_to_released_and_cancelled(
    postgres_session_factory,
):
    """Scenario: reconciliation proves an ambiguous external release completed.

    Given a cancelled external reservation whose release result became
    RELEASE_UNKNOWN,
    When ReconcileProviderWorkService receives a mocked NOT_RESERVED lookup,
    Then PostgreSQL marks the line RELEASED and completes the reservation as
    CANCELLED.
    """
    # Given
    source = await seed_external_source(
        postgres_session_factory,
        sku="SERVICE-RECONCILE-RELEASE",
    )
    provider = MockReservationProvider(
        release_outcome=ProviderReleaseOutcome.UNKNOWN,
        lookup_outcome=ProviderReservationLookupOutcome.RESERVED,
    )
    reservation_id = await _create_and_hold_external(
        postgres_session_factory,
        source,
        provider,
        key="service-reconcile-release",
    )
    cancel_service = CancelReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    )
    prepare_release_service = ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    )
    await cancel_service.execute(
        reservation_id,
        user_id="service-user",
    )
    await prepare_release_service.execute(reservation_id)

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        claimed = await uow.reservations.claim_pending_external_releases(
            limit=10,
            lease_seconds=60,
        )
        await uow.commit()

    release_service = ProcessClaimedProviderReleaseService(
        uow_factory=uow_factory(postgres_session_factory),
        providers=ProviderRegistry({source.provider_id: provider}),
    )
    await release_service.execute(claimed[0])

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        unknown = await uow.reservations.claim_unknown_external_releases(
            limit=10,
            lease_seconds=60,
        )
        await uow.commit()

    provider.lookup_outcome = ProviderReservationLookupOutcome.NOT_RESERVED
    reconcile_service = ReconcileProviderWorkService(
        uow_factory=uow_factory(postgres_session_factory),
        providers=ProviderRegistry({source.provider_id: provider}),
    )

    # When
    await reconcile_service.reconcile_release(unknown[0])

    # Then
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED
    assert reservation.release_reason == "USER_CANCELLED"
    assert line is not None
    assert line.status == ReservationLineStatus.RELEASED
    assert line.released_at is not None
    assert line.provider_claim_token is None
    assert line.provider_lease_until is None
