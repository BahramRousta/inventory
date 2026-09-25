from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.application.ports.provider_gateway import (
    ProviderHoldLookupOutcome,
    ProviderHoldOutcome,
    ProviderReleaseOutcome,
)
from app.application.services.claim_pending_provider_holds import (
    ClaimPendingProviderHoldsService,
)
from app.application.services.process_claimed_provider_release import (
    ProcessClaimedProviderReleaseService,
)
from app.application.services.process_pending_provider_hold import (
    ProcessPendingProviderHoldService,
)
from app.application.services.process_releasing_reservation import (
    ProcessReleasingReservationService,
)
from app.application.services.reconcile_provider_work import ReconcileProviderWorkService
from app.domain.enums import ReservationLineStatus, ReservationStatus
from app.infrastructure.db.models import OrderModel, ReservationLineModel, ReservationModel
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.infrastructure.providers.mock import MockReservationProviderGateway
from tests.e2e.support import (
    api_client,
    create_body,
    create_headers,
    reservation_registry,
    seed_external_source,
    uow_factory,
)


pytestmark = pytest.mark.postgres


async def _claim_and_process_hold(factory, registry):
    claimed = await ClaimPendingProviderHoldsService(
        uow_factory=uow_factory(factory),
        batch_size=10,
        lease_seconds=30,
    ).execute()
    assert len(claimed) == 1

    processed = await ProcessPendingProviderHoldService(
        uow_factory=uow_factory(factory),
        provider_gateways=registry,
    ).execute(claimed[0])
    assert processed is True
    return claimed[0]


async def test_mock_reservation_provider_hold_success_activates_reservation(
    postgres_session_factory,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-MOCK-SUCCESS",
    )
    gateway = MockReservationProviderGateway()
    registry = reservation_registry(source.provider_id, gateway)

    async with api_client(
        postgres_session_factory,
        provider_gateways=registry,
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="mock-success"),
            json=create_body(source),
        )

    assert created.status_code == 202
    work = await _claim_and_process_hold(postgres_session_factory, registry)

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, work.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )

    assert gateway.hold_calls == 1
    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert line is not None
    assert line.status == ReservationLineStatus.HELD
    assert line.external_hold_ref == (
        f"mock-hold:{work.reservation_id}:{source.source_id}:HOLD"
    )


async def test_mock_provider_decline_compensates_to_cancelled(
    postgres_session_factory,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-MOCK-DECLINE",
    )
    gateway = MockReservationProviderGateway(
        hold_outcome=ProviderHoldOutcome.DECLINED,
        lookup_outcome=ProviderHoldLookupOutcome.NOT_HELD,
    )
    registry = reservation_registry(source.provider_id, gateway)

    async with api_client(
        postgres_session_factory,
        provider_gateways=registry,
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="mock-decline"),
            json=create_body(source),
        )

    assert created.status_code == 202
    work = await _claim_and_process_hold(postgres_session_factory, registry)

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

    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED


async def test_unknown_hold_is_reconciled_to_active(
    postgres_session_factory,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-MOCK-UNKNOWN",
    )
    gateway = MockReservationProviderGateway(
        hold_outcome=ProviderHoldOutcome.UNKNOWN,
        lookup_outcome=ProviderHoldLookupOutcome.UNKNOWN,
    )
    registry = reservation_registry(source.provider_id, gateway)

    async with api_client(
        postgres_session_factory,
        provider_gateways=registry,
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="mock-unknown"),
            json=create_body(source),
        )

    assert created.status_code == 202
    work = await _claim_and_process_hold(postgres_session_factory, registry)

    async with postgres_session_factory() as session:
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )
    assert line is not None
    assert line.status == ReservationLineStatus.HOLD_UNKNOWN

    gateway.lookup_outcome = ProviderHoldLookupOutcome.HELD

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        unknown = await uow.reservations.claim_unknown_external_holds(
            limit=10,
            lease_seconds=30,
        )
        await uow.commit()
    assert len(unknown) == 1

    reconciled = await ReconcileProviderWorkService(
        uow_factory=uow_factory(postgres_session_factory),
        provider_gateways=registry,
    ).reconcile_hold(unknown[0])
    assert reconciled is True

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, work.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )

    assert gateway.lookup_calls == 1
    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert line is not None
    assert line.status == ReservationLineStatus.HELD
    assert line.external_hold_ref


async def test_external_cancel_uses_mock_release_and_finishes_cancelled(
    postgres_session_factory,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-MOCK-RELEASE",
    )
    gateway = MockReservationProviderGateway()
    registry = reservation_registry(source.provider_id, gateway)

    async with api_client(
        postgres_session_factory,
        provider_gateways=registry,
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="mock-release"),
            json=create_body(source),
        )

    work = await _claim_and_process_hold(postgres_session_factory, registry)

    async with api_client(
        postgres_session_factory,
        provider_gateways=registry,
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
        provider_gateways=registry,
    ).execute(releases[0])
    assert persisted is True

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, work.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )

    assert gateway.release_calls == 1
    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED
    assert line is not None
    assert line.status == ReservationLineStatus.RELEASED


async def test_release_unknown_requires_lookup_before_terminal_cancel(
    postgres_session_factory,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-MOCK-RELEASE-UNKNOWN",
    )
    gateway = MockReservationProviderGateway(
        release_outcome=ProviderReleaseOutcome.UNKNOWN,
        lookup_outcome=ProviderHoldLookupOutcome.HELD,
    )
    registry = reservation_registry(source.provider_id, gateway)

    async with api_client(
        postgres_session_factory,
        provider_gateways=registry,
    ) as client:
        await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="mock-release-unknown"),
            json=create_body(source),
        )

    work = await _claim_and_process_hold(postgres_session_factory, registry)

    async with api_client(
        postgres_session_factory,
        provider_gateways=registry,
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
        provider_gateways=registry,
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

    gateway.lookup_outcome = ProviderHoldLookupOutcome.NOT_HELD

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        unknown = await uow.reservations.claim_unknown_external_releases(
            limit=10,
            lease_seconds=30,
        )
        await uow.commit()
    assert len(unknown) == 1

    await ReconcileProviderWorkService(
        uow_factory=uow_factory(postgres_session_factory),
        provider_gateways=registry,
    ).reconcile_release(unknown[0])

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, work.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED
    assert line is not None
    assert line.status == ReservationLineStatus.RELEASED


async def test_payment_success_on_external_hold_creates_single_order(
    postgres_session_factory,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-MOCK-PAYMENT",
    )
    gateway = MockReservationProviderGateway()
    registry = reservation_registry(source.provider_id, gateway)

    async with api_client(
        postgres_session_factory,
        provider_gateways=registry,
    ) as client:
        await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="mock-payment"),
            json=create_body(source),
        )

    work = await _claim_and_process_hold(postgres_session_factory, registry)

    async with api_client(
        postgres_session_factory,
        provider_gateways=registry,
    ) as client:
        paid = await client.post(
            f"/reservations/{work.reservation_id}/payment-outcome",
            headers=create_headers(),
            json={"event_id": str(uuid4()), "outcome": "SUCCESS"},
        )

    assert paid.status_code == 200
    assert paid.json()["status"] == "CONFIRMED"

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, work.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )
        order_count = await session.scalar(
            select(func.count()).select_from(OrderModel)
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.CONFIRMED
    assert line is not None
    assert line.status == ReservationLineStatus.CONFIRMED
    assert order_count == 1


async def test_payment_failure_before_provider_call_never_invokes_mock_hold(
    postgres_session_factory,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-MOCK-NO-CALL",
    )
    gateway = MockReservationProviderGateway()
    registry = reservation_registry(source.provider_id, gateway)

    async with api_client(
        postgres_session_factory,
        provider_gateways=registry,
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="mock-no-call"),
            json=create_body(source),
        )
        reservation_id = UUID(created.json()["reservation_id"])
        failed = await client.post(
            f"/reservations/{reservation_id}/payment-outcome",
            headers=create_headers(),
            json={"event_id": str(uuid4()), "outcome": "FAILURE"},
        )

    assert failed.status_code == 202

    await ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    ).execute(reservation_id)

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )

    assert gateway.hold_calls == 0
    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED
    assert line is not None
    assert line.status == ReservationLineStatus.FAILED
