import pytest
from sqlalchemy import select

from app.application.ports.provider_gateway import (
    ProviderRegistry,
    ProviderReservationLookupOutcome,
    ProviderReserveOutcome,
    ProviderReleaseOutcome,
)
from app.application.services.create.claim_pending_provider_holds import (
    ClaimPendingProviderHoldsService,
)
from app.application.services.cancel.process_claimed_provider_release import (
    ProcessClaimedProviderReleaseService,
)
from app.application.services.create.process_pending_provider_hold import (
    ProcessPendingProviderHoldService,
)
from app.application.services.cancel.process_releasing_reservation import (
    ProcessReleasingReservationService,
)
from app.application.services.reconciliation.reconcile_provider_work import (
    ReconcileProviderWorkService,
)
from app.domain.enums import ReservationLineStatus, ReservationStatus
from app.infrastructure.db.models import ReservationLineModel, ReservationModel
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.infrastructure.providers.mock import (
    MockAvailabilityProvider,
    MockReservationProvider,
)
from tests.conftest import (
    api_client,
    create_body,
    create_headers,
    provider_registry,
    seed_external_source,
    uow_factory,
)


pytestmark = pytest.mark.postgres


async def _claim_and_process_reservation(factory, registry):
    claimed = await ClaimPendingProviderHoldsService(
        uow_factory=uow_factory(factory),
        batch_size=10,
        lease_seconds=30,
    ).execute()
    assert len(claimed) == 1

    processed = await ProcessPendingProviderHoldService(
        uow_factory=uow_factory(factory),
        providers=registry,
    ).execute(claimed[0])
    assert processed is True
    return claimed[0]


async def test_mock_reservation_provider_reserve_success_activates_reservation(
    postgres_session_factory,
):
    """Scenario: a successful external hold activates its reservation.

    Given an enabled external source and a provider that accepts the hold,
    When the API creates the reservation and the hold worker processes it,
    Then the line is HELD and the reservation becomes ACTIVE.
    """
    # Given
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-MOCK-SUCCESS",
    )
    gateway = MockReservationProvider()
    registry = provider_registry(source.provider_id, gateway)

    # When
    async with api_client(
        postgres_session_factory,
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="mock-success"),
            json=create_body(source),
        )

    assert created.status_code == 202
    work = await _claim_and_process_reservation(postgres_session_factory, registry)

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, work.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )

    # Then
    assert gateway.reserve_calls == 1
    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert line is not None
    assert line.status == ReservationLineStatus.HELD
    assert line.external_hold_ref == (
        f"mock-reservation:{work.reservation_id}:{source.source_id}:RESERVE"
    )


async def test_mock_provider_decline_compensates_to_cancelled(
    postgres_session_factory,
):
    """Scenario: a definitive provider decline cancels the reservation.

    Given an external provider configured to decline its hold,
    When hold processing and compensation run,
    Then the line is FAILED and the reservation finishes CANCELLED.
    """
    # Given
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-MOCK-DECLINE",
    )
    gateway = MockReservationProvider(
        reserve_outcome=ProviderReserveOutcome.DECLINED,
        lookup_outcome=ProviderReservationLookupOutcome.NOT_RESERVED,
    )
    registry = provider_registry(source.provider_id, gateway)

    # When
    async with api_client(
        postgres_session_factory,
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="mock-decline"),
            json=create_body(source),
        )

    assert created.status_code == 202
    work = await _claim_and_process_reservation(postgres_session_factory, registry)

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, work.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.RELEASING
    assert reservation.release_reason == "CREATE_FAILED"
    assert line is not None
    assert line.status == ReservationLineStatus.FAILED

    await ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    ).execute(work.reservation_id)

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, work.reservation_id)

    # Then
    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED


async def test_unknown_reservation_is_reconciled_to_active(
    postgres_session_factory,
):
    """Scenario: reconciliation resolves an ambiguous hold as successful.

    Given a provider hold that returns UNKNOWN and later reports RESERVED,
    When the hold and reconciliation workers process the line,
    Then the line becomes HELD and the reservation becomes ACTIVE.
    """
    # Given
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-MOCK-UNKNOWN",
    )
    gateway = MockReservationProvider(
        reserve_outcome=ProviderReserveOutcome.UNKNOWN,
        lookup_outcome=ProviderReservationLookupOutcome.UNKNOWN,
    )
    registry = provider_registry(source.provider_id, gateway)

    # When
    async with api_client(
        postgres_session_factory,
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="mock-unknown"),
            json=create_body(source),
        )

    assert created.status_code == 202
    work = await _claim_and_process_reservation(postgres_session_factory, registry)

    async with postgres_session_factory() as session:
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )
    assert line is not None
    assert line.status == ReservationLineStatus.HOLD_UNKNOWN

    gateway.lookup_outcome = ProviderReservationLookupOutcome.RESERVED

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        unknown = await uow.reservations.claim_unknown_external_holds(
            limit=10,
            lease_seconds=30,
        )
        await uow.commit()
    assert len(unknown) == 1

    reconciled = await ReconcileProviderWorkService(
        uow_factory=uow_factory(postgres_session_factory),
        providers=registry,
    ).reconcile_hold(unknown[0])
    assert reconciled is True

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, work.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )

    # Then
    assert gateway.lookup_calls == 1
    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert line is not None
    assert line.status == ReservationLineStatus.HELD
    assert line.external_hold_ref


async def test_external_cancel_uses_mock_release_and_finishes_cancelled(
    postgres_session_factory,
):
    """Scenario: cancelling an external hold releases provider inventory.

    Given an ACTIVE reservation backed by a successful external hold,
    When the owner cancels it and the release worker calls the provider,
    Then the line is RELEASED and the reservation becomes CANCELLED.
    """
    # Given
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-MOCK-RELEASE",
    )
    gateway = MockReservationProvider()
    registry = provider_registry(source.provider_id, gateway)

    # When
    async with api_client(
        postgres_session_factory,
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="mock-release"),
            json=create_body(source),
        )

    work = await _claim_and_process_reservation(postgres_session_factory, registry)

    async with api_client(
        postgres_session_factory,
    ) as client:
        cancelled = await client.post(
            f"/reservations/{work.reservation_id}/cancel",
            headers=create_headers(),
        )

    assert cancelled.status_code == 202

    await ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    ).execute(work.reservation_id)

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        releases = await uow.reservations.claim_pending_external_releases(
            limit=10,
            lease_seconds=30,
        )
        await uow.commit()
    assert len(releases) == 1

    persisted = await ProcessClaimedProviderReleaseService(
        uow_factory=uow_factory(postgres_session_factory),
        providers=registry,
    ).execute(releases[0])
    assert persisted is True

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, work.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )

    # Then
    assert gateway.release_calls == 1
    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED
    assert line is not None
    assert line.status == ReservationLineStatus.RELEASED


async def test_release_unknown_requires_lookup_before_terminal_cancel(
    postgres_session_factory,
):
    """Scenario: an ambiguous release requires provider inquiry.

    Given an external release that returns UNKNOWN but later reports not reserved,
    When release processing and reconciliation run,
    Then the reservation remains RELEASING until inquiry proves it can be CANCELLED.
    """
    # Given
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-MOCK-RELEASE-UNKNOWN",
    )
    gateway = MockReservationProvider(
        release_outcome=ProviderReleaseOutcome.UNKNOWN,
        lookup_outcome=ProviderReservationLookupOutcome.RESERVED,
    )
    registry = provider_registry(source.provider_id, gateway)

    # When
    async with api_client(
        postgres_session_factory,
    ) as client:
        await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="mock-release-unknown"),
            json=create_body(source),
        )

    work = await _claim_and_process_reservation(postgres_session_factory, registry)

    async with api_client(
        postgres_session_factory,
    ) as client:
        await client.post(
            f"/reservations/{work.reservation_id}/cancel",
            headers=create_headers(),
        )

    await ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    ).execute(work.reservation_id)

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        releases = await uow.reservations.claim_pending_external_releases(
            limit=10,
            lease_seconds=30,
        )
        await uow.commit()

    await ProcessClaimedProviderReleaseService(
        uow_factory=uow_factory(postgres_session_factory),
        providers=registry,
    ).execute(releases[0])

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, work.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )
    assert reservation is not None
    assert reservation.status == ReservationStatus.RELEASING
    assert line is not None
    assert line.status == ReservationLineStatus.RELEASE_UNKNOWN

    gateway.lookup_outcome = ProviderReservationLookupOutcome.NOT_RESERVED

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        unknown = await uow.reservations.claim_unknown_external_releases(
            limit=10,
            lease_seconds=30,
        )
        await uow.commit()
    assert len(unknown) == 1

    await ReconcileProviderWorkService(
        uow_factory=uow_factory(postgres_session_factory),
        providers=registry,
    ).reconcile_release(unknown[0])

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, work.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )

    # Then
    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED
    assert line is not None
    assert line.status == ReservationLineStatus.RELEASED


async def test_query_provider_reserve_uses_availability_and_activates_reservation(
    postgres_session_factory,
):
    """Scenario: sufficient advisory availability activates a query-provider line.

    Given a query-style provider reporting enough available quantity,
    When its pending hold is processed,
    Then the line is HELD and the reservation becomes ACTIVE.
    """
    # Given
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-QUERY-PROVIDER",
    )
    provider = MockAvailabilityProvider(available_quantity=5)
    registry = provider_registry(source.provider_id, provider)

    # When
    async with api_client(postgres_session_factory) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="query-provider"),
            json=create_body(source, quantity=2),
        )

    assert created.status_code == 202

    work = await _claim_and_process_reservation(
        postgres_session_factory,
        registry,
    )

    async with postgres_session_factory() as session:
        reservation = await session.get(
            ReservationModel,
            work.reservation_id,
        )
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )

    # Then
    assert provider.reserve_calls == 1
    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert line is not None
    assert line.status == ReservationLineStatus.HELD
    assert line.external_hold_ref == (f"query:{work.reservation_id}:{source.source_id}:RESERVE")


async def test_query_provider_declines_when_availability_is_insufficient(
    postgres_session_factory,
):
    """Scenario: insufficient advisory availability declines a query-provider line.

    Given a query-style provider reporting less stock than requested,
    When its pending hold is processed,
    Then the line is FAILED and the reservation enters RELEASING.
    """
    # Given
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-QUERY-INSUFFICIENT",
    )
    provider = MockAvailabilityProvider(available_quantity=1)
    registry = provider_registry(source.provider_id, provider)

    # When
    async with api_client(postgres_session_factory) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="query-insufficient"),
            json=create_body(source, quantity=2),
        )

    assert created.status_code == 202

    work = await _claim_and_process_reservation(
        postgres_session_factory,
        registry,
    )

    async with postgres_session_factory() as session:
        reservation = await session.get(
            ReservationModel,
            work.reservation_id,
        )
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )

    # Then
    assert provider.reserve_calls == 1
    assert reservation is not None
    assert reservation.status == ReservationStatus.RELEASING
    assert reservation.release_reason == "CREATE_FAILED"
    assert line is not None
    assert line.status == ReservationLineStatus.FAILED


async def test_missing_provider_is_rejected_during_provider_processing(
    postgres_session_factory,
):
    """Scenario: missing runtime provider registration fails the external line.

    Given an external source whose provider is absent from the runtime registry,
    When the hold worker processes its pending line,
    Then the line is FAILED and the reservation enters RELEASING.
    """
    # Given
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-NO-PROVIDER",
    )

    # When
    async with api_client(postgres_session_factory) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="missing-provider"),
            json=create_body(source),
        )

    assert created.status_code == 202

    work = await _claim_and_process_reservation(
        postgres_session_factory,
        ProviderRegistry(),
    )

    async with postgres_session_factory() as session:
        reservation = await session.get(
            ReservationModel,
            work.reservation_id,
        )
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )

    # Then
    assert reservation is not None
    assert reservation.status == ReservationStatus.RELEASING
    assert reservation.release_reason == "CREATE_FAILED"
    assert line is not None
    assert line.status == ReservationLineStatus.FAILED
