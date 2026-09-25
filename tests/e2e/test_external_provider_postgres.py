from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.application.ports.provider_gateway import InMemoryProviderGatewayRegistry
from app.application.services.claim_pending_provider_holds import (
    ClaimPendingProviderHoldsService,
)
from app.application.services.expire_reserving_reservation import (
    ExpireReservingReservationService,
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
from app.infrastructure.db.models import (
    InternalStockModel,
    OrderLineModel,
    OrderModel,
    ReservationLineModel,
    ReservationModel,
)
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.infrastructure.providers.external_hold_http import ExternalHoldHttpGateway
from tests.e2e.support import (
    api_client,
    create_body,
    create_headers,
    seed_external_source,
    seed_internal_source,
    uow_factory,
)


pytestmark = pytest.mark.postgres


def _registry(provider_id, base_url, *, timeout=0.1):
    return InMemoryProviderGatewayRegistry(
        {
            provider_id: ExternalHoldHttpGateway(
                base_url=base_url,
                hold_timeout_seconds=timeout,
            )
        }
    )


async def _set_provider_mode(base_url: str, mode: str) -> None:
    async with httpx.AsyncClient(timeout=1.0) as client:
        response = await client.post(f"{base_url}/admin/mode/{mode}")
    assert response.status_code == 200


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


async def test_external_hold_success_activates_reservation_and_persists_provider_reference(
    postgres_session_factory,
    fake_provider_url,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-HOLD-OK",
    )
    registry = _registry(source.provider_id, fake_provider_url)

    async with api_client(
        postgres_session_factory, provider_gateways=registry
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="ext-hold-ok"),
            json=create_body(source),
        )

    assert created.status_code == 202
    assert created.json()["status"] == "RESERVING"

    work = await _claim_and_process_hold(postgres_session_factory, registry)
    reservation_id = work.reservation_id
    hold_key = f"{reservation_id}:{source.source_id}:HOLD"

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
    assert line.external_hold_ref

    async with httpx.AsyncClient(timeout=1.0) as client:
        provider_state = await client.get(f"{fake_provider_url}/holds/{hold_key}")
    assert provider_state.status_code == 200
    assert provider_state.json()["status"] == "HELD"
    assert provider_state.json()["hold_ref"] == line.external_hold_ref


async def test_external_definitive_decline_moves_to_releasing_then_cancelled(
    postgres_session_factory,
    fake_provider_url,
):
    await _set_provider_mode(fake_provider_url, "decline")
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-DECLINE",
    )
    registry = _registry(source.provider_id, fake_provider_url)

    async with api_client(
        postgres_session_factory, provider_gateways=registry
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="ext-decline"),
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
    assert line.external_hold_ref is None

    await ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    ).execute(work.reservation_id)

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, work.reservation_id)
        order_count = await session.scalar(
            select(func.count()).select_from(OrderModel)
        )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED
    assert reservation.release_reason == "CREATE_FAILED"
    assert order_count == 0


async def test_timeout_after_provider_side_effect_stays_unknown_until_reconciliation(
    postgres_session_factory,
    fake_provider_url,
):
    await _set_provider_mode(fake_provider_url, "timeout_after_side_effect")
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-TIMEOUT",
    )
    registry = _registry(source.provider_id, fake_provider_url, timeout=0.05)

    async with api_client(
        postgres_session_factory, provider_gateways=registry
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="ext-timeout"),
            json=create_body(source),
        )

    assert created.status_code == 202
    work = await _claim_and_process_hold(postgres_session_factory, registry)
    hold_key = f"{work.reservation_id}:{source.source_id}:HOLD"

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, work.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )
    assert reservation is not None
    assert reservation.status == ReservationStatus.RESERVING
    assert line is not None
    assert line.status == ReservationLineStatus.HOLD_UNKNOWN

    async with api_client(
        postgres_session_factory, provider_gateways=registry
    ) as client:
        unknown_snapshot = await client.get(
            f"/reservations/{work.reservation_id}",
            headers=create_headers(user_id="user-1"),
        )
    assert unknown_snapshot.status_code == 200
    assert unknown_snapshot.json()["requires_attention"] is True
    assert unknown_snapshot.json()["payment_allowed"] is False

    async with httpx.AsyncClient(timeout=1.0) as client:
        provider_state = await client.get(f"{fake_provider_url}/holds/{hold_key}")
    assert provider_state.status_code == 200
    assert provider_state.json()["status"] == "HELD"

    await _set_provider_mode(fake_provider_url, "success")
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
    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert line is not None
    assert line.status == ReservationLineStatus.HELD
    assert line.external_hold_ref == provider_state.json()["hold_ref"]

    async with api_client(
        postgres_session_factory, provider_gateways=registry
    ) as client:
        reconciled_snapshot = await client.get(
            f"/reservations/{work.reservation_id}",
            headers=create_headers(user_id="user-1"),
        )
    assert reconciled_snapshot.status_code == 200
    assert reconciled_snapshot.json()["requires_attention"] is False
    assert reconciled_snapshot.json()["payment_allowed"] is True


async def test_external_cancel_releases_remote_hold_and_finishes_cancelled(
    postgres_session_factory,
    fake_provider_url,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-RELEASE",
    )
    registry = _registry(source.provider_id, fake_provider_url)

    async with api_client(
        postgres_session_factory, provider_gateways=registry
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="ext-release-create"),
            json=create_body(source),
        )
        assert created.status_code == 202

    work = await _claim_and_process_hold(postgres_session_factory, registry)
    hold_key = f"{work.reservation_id}:{source.source_id}:HOLD"

    async with api_client(
        postgres_session_factory, provider_gateways=registry
    ) as client:
        cancelled = await client.post(
            f"/reservations/{work.reservation_id}/cancel",
            headers=create_headers(user_id="user-1"),
        )
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "RELEASING"

    prepared = await ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    ).execute(work.reservation_id)
    assert prepared is True

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        releases = await uow.reservations.claim_pending_external_releases(
            limit=10,
            lease_seconds=30,
        )
        await uow.commit()
    assert len(releases) == 1

    released = await ProcessClaimedProviderReleaseService(
        uow_factory=uow_factory(postgres_session_factory),
        provider_gateways=registry,
    ).execute(releases[0])
    assert released is True

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

    async with httpx.AsyncClient(timeout=1.0) as client:
        provider_state = await client.get(f"{fake_provider_url}/holds/{hold_key}")
    assert provider_state.status_code == 200
    assert provider_state.json()["status"] == "RELEASED"


async def test_mixed_source_external_decline_compensates_internal_hold_and_creates_no_order(
    postgres_session_factory,
    fake_provider_url,
):
    await _set_provider_mode(fake_provider_url, "decline")
    internal = await seed_internal_source(
        postgres_session_factory,
        sku="MIXED-INTERNAL",
        on_hand=2,
    )
    external = await seed_external_source(
        postgres_session_factory,
        sku="MIXED-EXTERNAL",
    )
    registry = _registry(external.provider_id, fake_provider_url)

    body = {
        "items": [
            {
                "product_id": str(internal.product_id),
                "stock_source_id": str(internal.source_id),
                "quantity": 1,
            },
            {
                "product_id": str(external.product_id),
                "stock_source_id": str(external.source_id),
                "quantity": 1,
            },
        ]
    }

    async with api_client(
        postgres_session_factory, provider_gateways=registry
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="mixed-decline"),
            json=body,
        )
    assert created.status_code == 202
    reservation_id = UUID(created.json()["reservation_id"])

    await _claim_and_process_hold(postgres_session_factory, registry)

    async with postgres_session_factory() as session:
        stock = await session.get(InternalStockModel, internal.source_id)
        reservation = await session.get(ReservationModel, reservation_id)
    assert stock is not None
    assert stock.held == 1
    assert reservation is not None
    assert reservation.status == ReservationStatus.RELEASING

    await ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    ).execute(reservation_id)

    async with postgres_session_factory() as session:
        stock = await session.get(InternalStockModel, internal.source_id)
        reservation = await session.get(ReservationModel, reservation_id)
        lines = (
            await session.scalars(
                select(ReservationLineModel)
                .where(ReservationLineModel.reservation_id == reservation_id)
                .order_by(ReservationLineModel.stock_source_id)
            )
        ).all()
        order_count = await session.scalar(
            select(func.count()).select_from(OrderModel)
        )

    assert stock is not None
    assert stock.held == 0
    assert stock.on_hand == 2
    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED
    assert {line.status for line in lines} == {
        ReservationLineStatus.FAILED,
        ReservationLineStatus.RELEASED,
    }
    assert order_count == 0


async def test_external_payment_success_uses_hold_as_final_allocation_and_snapshots_reference(
    postgres_session_factory,
    fake_provider_url,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-PAY",
    )
    registry = _registry(source.provider_id, fake_provider_url)

    async with api_client(
        postgres_session_factory, provider_gateways=registry
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="ext-pay-create"),
            json=create_body(source),
        )
    work = await _claim_and_process_hold(postgres_session_factory, registry)

    async with postgres_session_factory() as session:
        held_line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )
    assert held_line is not None
    assert held_line.external_hold_ref
    original_ref = held_line.external_hold_ref

    event_id = uuid4()
    async with api_client(
        postgres_session_factory, provider_gateways=registry
    ) as client:
        paid = await client.post(
            f"/reservations/{work.reservation_id}/payment-outcome",
            headers=create_headers(user_id="user-1"),
            json={"event_id": str(event_id), "outcome": "SUCCESS"},
        )

    assert paid.status_code == 200
    assert paid.json()["status"] == "CONFIRMED"
    order_id = UUID(paid.json()["order_id"])

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, work.reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )
        order_line = await session.scalar(
            select(OrderLineModel).where(OrderLineModel.order_id == order_id)
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.CONFIRMED
    assert line is not None
    assert line.status == ReservationLineStatus.CONFIRMED
    assert order_line is not None
    assert order_line.provider_id == source.provider_id
    assert order_line.provider_allocation_ref == original_ref

    hold_key = f"{work.reservation_id}:{source.source_id}:HOLD"
    async with httpx.AsyncClient(timeout=1.0) as client:
        provider_state = await client.get(f"{fake_provider_url}/holds/{hold_key}")
    assert provider_state.status_code == 200
    assert provider_state.json()["status"] == "HELD"
    assert provider_state.json()["hold_ref"] == original_ref


async def test_pending_external_create_replay_stays_202_and_does_not_duplicate_work(
    postgres_session_factory,
    fake_provider_url,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-PENDING-REPLAY",
    )
    registry = _registry(source.provider_id, fake_provider_url)

    async with api_client(
        postgres_session_factory, provider_gateways=registry
    ) as client:
        first = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="ext-pending-replay"),
            json=create_body(source),
        )
        second = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="ext-pending-replay"),
            json=create_body(source),
        )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.headers["retry-after"] == "1"
    assert second.headers["retry-after"] == "1"
    assert first.json()["reservation_id"] == second.json()["reservation_id"]

    reservation_id = UUID(first.json()["reservation_id"])
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        lines = (
            await session.scalars(
                select(ReservationLineModel).where(
                    ReservationLineModel.reservation_id == reservation_id
                )
            )
        ).all()
    assert reservation is not None
    assert reservation.status == ReservationStatus.RESERVING
    assert len(lines) == 1
    assert lines[0].status == ReservationLineStatus.HOLD_PENDING


async def test_payment_failure_before_external_hold_claim_finishes_cancelled_without_remote_hold(
    postgres_session_factory,
    fake_provider_url,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-PAY-FAIL-PENDING",
    )
    registry = _registry(source.provider_id, fake_provider_url)
    event_id = uuid4()

    async with api_client(
        postgres_session_factory, provider_gateways=registry
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="ext-pay-fail-pending"),
            json=create_body(source),
        )
        reservation_id = UUID(created.json()["reservation_id"])
        failed = await client.post(
            f"/reservations/{reservation_id}/payment-outcome",
            headers=create_headers(user_id="user-1"),
            json={"event_id": str(event_id), "outcome": "FAILURE"},
        )

    assert created.status_code == 202
    assert failed.status_code == 202
    assert failed.json()["status"] == "RELEASING"

    processed = await ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    ).execute(reservation_id)
    assert processed is True

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )
        order_count = await session.scalar(
            select(func.count()).select_from(OrderModel)
        )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED
    assert reservation.release_reason == "PAYMENT_FAILED"
    assert line is not None
    assert line.status == ReservationLineStatus.FAILED
    assert line.external_hold_ref is None
    assert order_count == 0

    hold_key = f"{reservation_id}:{source.source_id}:HOLD"
    async with httpx.AsyncClient(timeout=1.0) as client:
        provider_state = await client.get(f"{fake_provider_url}/holds/{hold_key}")
    assert provider_state.status_code == 404


async def test_cancel_before_external_hold_claim_finishes_cancelled_without_provider_call(
    postgres_session_factory,
    fake_provider_url,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-CANCEL-PENDING",
    )
    registry = _registry(source.provider_id, fake_provider_url)

    async with api_client(
        postgres_session_factory, provider_gateways=registry
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="ext-cancel-pending"),
            json=create_body(source),
        )
        reservation_id = UUID(created.json()["reservation_id"])
        cancelled = await client.post(
            f"/reservations/{reservation_id}/cancel",
            headers=create_headers(user_id="user-1"),
        )

    assert created.status_code == 202
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "RELEASING"

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
    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED
    assert reservation.release_reason == "USER_CANCELLED"
    assert line is not None
    assert line.status == ReservationLineStatus.FAILED

    hold_key = f"{reservation_id}:{source.source_id}:HOLD"
    async with httpx.AsyncClient(timeout=1.0) as client:
        provider_state = await client.get(f"{fake_provider_url}/holds/{hold_key}")
    assert provider_state.status_code == 404


async def test_expiry_before_external_hold_claim_finishes_expired_without_provider_call(
    postgres_session_factory,
    fake_provider_url,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-EXPIRE-PENDING",
    )
    registry = _registry(source.provider_id, fake_provider_url)

    async with api_client(
        postgres_session_factory, provider_gateways=registry
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="ext-expire-pending"),
            json=create_body(source),
        )
    assert created.status_code == 202
    reservation_id = UUID(created.json()["reservation_id"])

    from datetime import datetime, timedelta, timezone
    from sqlalchemy import update

    async with postgres_session_factory.begin() as session:
        await session.execute(
            update(ReservationModel)
            .where(ReservationModel.id == reservation_id)
            .values(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
            )
        )

    claimed = await ExpireReservingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    ).execute_batch(limit=10)
    assert reservation_id in claimed

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
    assert reservation is not None
    assert reservation.status == ReservationStatus.EXPIRED
    assert reservation.release_reason == "EXPIRED"
    assert line is not None
    assert line.status == ReservationLineStatus.FAILED

    hold_key = f"{reservation_id}:{source.source_id}:HOLD"
    async with httpx.AsyncClient(timeout=1.0) as client:
        provider_state = await client.get(f"{fake_provider_url}/holds/{hold_key}")
    assert provider_state.status_code == 404


async def test_payment_failure_while_hold_unknown_reconciles_then_releases_real_remote_hold(
    postgres_session_factory,
    fake_provider_url,
):
    await _set_provider_mode(fake_provider_url, "timeout_after_side_effect")
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXT-UNKNOWN-THEN-FAIL",
    )
    registry = _registry(source.provider_id, fake_provider_url, timeout=0.05)

    async with api_client(
        postgres_session_factory, provider_gateways=registry
    ) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="unknown-then-fail"),
            json=create_body(source),
        )
    assert created.status_code == 202

    work = await _claim_and_process_hold(postgres_session_factory, registry)
    hold_key = f"{work.reservation_id}:{source.source_id}:HOLD"

    async with postgres_session_factory() as session:
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )
        reservation = await session.get(ReservationModel, work.reservation_id)
    assert line is not None
    assert line.status == ReservationLineStatus.HOLD_UNKNOWN
    assert reservation is not None
    assert reservation.status == ReservationStatus.RESERVING

    event_id = uuid4()
    async with api_client(
        postgres_session_factory, provider_gateways=registry
    ) as client:
        failed = await client.post(
            f"/reservations/{work.reservation_id}/payment-outcome",
            headers=create_headers(user_id="user-1"),
            json={"event_id": str(event_id), "outcome": "FAILURE"},
        )
    assert failed.status_code == 202
    assert failed.json()["status"] == "RELEASING"
    assert failed.json()["requires_attention"] is True

    # Compensation cannot guess whether the timed-out HOLD happened.
    await ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    ).execute(work.reservation_id)

    async with postgres_session_factory() as session:
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )
        reservation = await session.get(ReservationModel, work.reservation_id)
    assert line is not None
    assert line.status == ReservationLineStatus.HOLD_UNKNOWN
    assert reservation is not None
    assert reservation.status == ReservationStatus.RELEASING

    await _set_provider_mode(fake_provider_url, "success")
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
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )
        reservation = await session.get(ReservationModel, work.reservation_id)
    assert line is not None
    assert line.status == ReservationLineStatus.RELEASE_PENDING
    assert line.external_hold_ref
    assert reservation is not None
    assert reservation.status == ReservationStatus.RELEASING

    async with SqlAlchemyUnitOfWork(postgres_session_factory) as uow:
        releases = await uow.reservations.claim_pending_external_releases(
            limit=10,
            lease_seconds=30,
        )
        await uow.commit()
    assert len(releases) == 1

    released = await ProcessClaimedProviderReleaseService(
        uow_factory=uow_factory(postgres_session_factory),
        provider_gateways=registry,
    ).execute(releases[0])
    assert released is True

    async with postgres_session_factory() as session:
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == work.reservation_id
            )
        )
        reservation = await session.get(ReservationModel, work.reservation_id)
    assert line is not None
    assert line.status == ReservationLineStatus.RELEASED
    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED
    assert reservation.release_reason == "PAYMENT_FAILED"

    async with httpx.AsyncClient(timeout=1.0) as client:
        provider_state = await client.get(f"{fake_provider_url}/holds/{hold_key}")
    assert provider_state.status_code == 200
    assert provider_state.json()["status"] == "RELEASED"
