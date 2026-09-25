from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from app.application.services.expire_reserving_reservation import (
    ExpireReservingReservationService,
)
from app.application.services.process_releasing_reservation import (
    ProcessReleasingReservationService,
)
from app.domain.enums import (
    PaymentOutcome,
    ReservationLineStatus,
    ReservationStatus,
)
from app.infrastructure.db.models import (
    InternalStockModel,
    OrderLineModel,
    OrderModel,
    PaymentEventModel,
    ProductModel,
    ReservationLineModel,
    ReservationModel,
)
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from tests.e2e.support import (
    api_client,
    create_body,
    create_headers,
    reservation_count,
    reservation_line_count,
    seed_external_source,
    seed_internal_source,
    uow_factory,
)


pytestmark = pytest.mark.postgres


async def _create_internal(client, source, *, user="user-1", key="create-1", quantity=1):
    return await client.post(
        "/reservations",
        headers=create_headers(user_id=user, idempotency_key=key),
        json=create_body(source, quantity=quantity),
    )


async def test_health_is_available_and_does_not_mutate_database(postgres_session_factory):
    async with api_client(postgres_session_factory) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert await reservation_count(postgres_session_factory) == 0


async def test_create_internal_reservation_returns_201_and_holds_stock(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="CREATE-OK", on_hand=5
    )

    async with api_client(postgres_session_factory) as client:
        response = await _create_internal(client, source, quantity=2)

    assert response.status_code == 201
    assert response.headers["location"].startswith("/reservations/")
    body = response.json()
    reservation_id = UUID(body["reservation_id"])
    assert body["status"] == "ACTIVE"
    assert body["payment_allowed"] is True
    assert body["requires_attention"] is False
    assert len(body["lines"]) == 1

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )
        stock = await session.get(InternalStockModel, source.source_id)

    assert reservation is not None
    assert reservation.user_id == "user-1"
    assert reservation.request_fingerprint
    assert reservation.status == ReservationStatus.ACTIVE
    assert line is not None
    assert line.quantity == 2
    assert line.status == ReservationLineStatus.HELD
    assert stock is not None
    assert stock.on_hand == 5
    assert stock.held == 2


async def test_duplicate_request_lines_are_canonicalized_before_stock_mutation(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="DUP-LINES", on_hand=10
    )
    body = {
        "items": [
            {
                "product_id": str(source.product_id),
                "stock_source_id": str(source.source_id),
                "quantity": 1,
            },
            {
                "product_id": str(source.product_id),
                "stock_source_id": str(source.source_id),
                "quantity": 2,
            },
        ]
    }

    async with api_client(postgres_session_factory) as client:
        response = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="dup-lines"),
            json=body,
        )

    assert response.status_code == 201
    reservation_id = UUID(response.json()["reservation_id"])

    async with postgres_session_factory() as session:
        lines = (
            await session.scalars(
                select(ReservationLineModel).where(
                    ReservationLineModel.reservation_id == reservation_id
                )
            )
        ).all()
        stock = await session.get(InternalStockModel, source.source_id)

    assert len(lines) == 1
    assert lines[0].quantity == 3
    assert lines[0].status == ReservationLineStatus.HELD
    assert stock is not None
    assert stock.held == 3
    assert await reservation_line_count(postgres_session_factory) == 1


async def test_idempotent_create_replay_returns_200_and_does_not_hold_twice(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="IDEMPOTENT", on_hand=5
    )

    async with api_client(postgres_session_factory) as client:
        first = await _create_internal(
            client, source, key="same-create-key", quantity=2
        )
        second = await _create_internal(
            client, source, key="same-create-key", quantity=2
        )

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["reservation_id"] == second.json()["reservation_id"]

    async with postgres_session_factory() as session:
        stock = await session.get(InternalStockModel, source.source_id)
        reservations = await session.scalar(
            select(func.count()).select_from(ReservationModel)
        )

    assert reservations == 1
    assert stock is not None
    assert stock.held == 2


async def test_idempotency_key_with_changed_body_returns_conflict_without_mutation(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="IDEM-CONFLICT", on_hand=5
    )

    async with api_client(postgres_session_factory) as client:
        first = await _create_internal(
            client, source, key="body-key", quantity=1
        )
        conflict = await _create_internal(
            client, source, key="body-key", quantity=2
        )

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"

    async with postgres_session_factory() as session:
        stock = await session.get(InternalStockModel, source.source_id)
        line = await session.scalar(select(ReservationLineModel))

    assert await reservation_count(postgres_session_factory) == 1
    assert stock is not None
    assert stock.held == 1
    assert line is not None
    assert line.quantity == 1


async def test_insufficient_internal_stock_rolls_back_reservation_and_hold(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="NO-STOCK", on_hand=1
    )

    async with api_client(postgres_session_factory) as client:
        response = await _create_internal(
            client, source, key="too-many", quantity=2
        )

    assert response.status_code == 409
    assert response.json()["code"] == "INSUFFICIENT_STOCK"

    async with postgres_session_factory() as session:
        stock = await session.get(InternalStockModel, source.source_id)

    assert await reservation_count(postgres_session_factory) == 0
    assert await reservation_line_count(postgres_session_factory) == 0
    assert stock is not None
    assert stock.held == 0
    assert stock.on_hand == 1


async def test_product_source_mismatch_returns_422_without_database_mutation(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="MISMATCH", on_hand=5
    )
    wrong_product_id = uuid4()
    async with postgres_session_factory.begin() as session:
        session.add(
            ProductModel(
                id=wrong_product_id,
                sku="WRONG-PRODUCT",
                name="Wrong product",
            )
        )

    async with api_client(postgres_session_factory) as client:
        response = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="mismatch"),
            json={
                "items": [
                    {
                        "product_id": str(wrong_product_id),
                        "stock_source_id": str(source.source_id),
                        "quantity": 1,
                    }
                ]
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "PRODUCT_SOURCE_MISMATCH"

    async with postgres_session_factory() as session:
        stock = await session.get(InternalStockModel, source.source_id)

    assert await reservation_count(postgres_session_factory) == 0
    assert stock is not None
    assert stock.held == 0


@pytest.mark.parametrize(
    ("source_enabled", "provider_enabled"),
    [(False, True), (True, False)],
)
async def test_disabled_source_or_provider_is_rejected_before_reservation(
    postgres_session_factory,
    source_enabled,
    provider_enabled,
):
    source = await seed_internal_source(
        postgres_session_factory,
        sku=f"DISABLED-{source_enabled}-{provider_enabled}",
        on_hand=5,
        source_enabled=source_enabled,
        provider_enabled=provider_enabled,
    )

    async with api_client(postgres_session_factory) as client:
        response = await _create_internal(
            client, source, key=f"disabled-{source_enabled}-{provider_enabled}"
        )

    assert response.status_code == 422
    assert response.json()["code"] == "SOURCE_DISABLED"
    assert await reservation_count(postgres_session_factory) == 0

    async with postgres_session_factory() as session:
        stock = await session.get(InternalStockModel, source.source_id)
    assert stock is not None
    assert stock.held == 0


@pytest.mark.parametrize(
    "headers",
    [
        {"X-User-Id": "user-1"},
        {"Idempotency-Key": "missing-user"},
    ],
)
async def test_create_requires_verified_user_and_idempotency_headers(
    postgres_session_factory,
    headers,
):
    source = await seed_internal_source(
        postgres_session_factory, sku=f"HEADER-{len(headers)}", on_hand=5
    )

    async with api_client(postgres_session_factory) as client:
        response = await client.post(
            "/reservations",
            headers=headers,
            json=create_body(source),
        )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert await reservation_count(postgres_session_factory) == 0

    async with postgres_session_factory() as session:
        stock = await session.get(InternalStockModel, source.source_id)
    assert stock is not None
    assert stock.held == 0


async def test_external_provider_without_required_capabilities_is_rejected(
    postgres_session_factory,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="QUERY-ONLY",
        supports_hold=False,
        supports_release=False,
        supports_get_hold=False,
        hold_is_final_allocation=False,
    )

    async with api_client(postgres_session_factory) as client:
        response = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="unsupported-external"),
            json=create_body(source),
        )

    assert response.status_code == 422
    assert response.json()["code"] == "SOURCE_NOT_RESERVABLE"
    assert await reservation_count(postgres_session_factory) == 0

    async with postgres_session_factory() as session:
        lines = await session.scalar(
            select(func.count()).select_from(ReservationLineModel)
        )
    assert lines == 0


async def test_external_provider_without_gateway_is_rejected_before_creation(
    postgres_session_factory,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="NO-GATEWAY",
    )

    async with api_client(postgres_session_factory) as client:
        response = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="no-gateway"),
            json=create_body(source),
        )

    assert response.status_code == 422
    assert response.json()["code"] == "SOURCE_NOT_RESERVABLE"
    assert await reservation_count(postgres_session_factory) == 0

    async with postgres_session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(ReservationLineModel)
        )
    assert count == 0


async def test_get_reservation_returns_persisted_snapshot_for_owner(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="GET-OWNER", on_hand=3
    )

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="get-owner")
        reservation_id = created.json()["reservation_id"]
        response = await client.get(
            f"/reservations/{reservation_id}",
            headers=create_headers(user_id="user-1"),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["reservation_id"] == reservation_id
    assert body["status"] == "ACTIVE"
    assert body["created_at"]
    assert body["expires_at"]

    async with postgres_session_factory() as session:
        row = await session.get(ReservationModel, UUID(reservation_id))
    assert row is not None
    assert row.status.value == body["status"]
    assert row.user_id == "user-1"


async def test_get_reservation_hides_other_users_reservation(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="GET-WRONG-OWNER", on_hand=3
    )

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="owner-create")
        reservation_id = created.json()["reservation_id"]
        response = await client.get(
            f"/reservations/{reservation_id}",
            headers=create_headers(user_id="user-2"),
        )

    assert response.status_code == 404
    assert response.json()["code"] == "RESERVATION_NOT_FOUND"

    async with postgres_session_factory() as session:
        row = await session.get(ReservationModel, UUID(reservation_id))
        stock = await session.get(InternalStockModel, source.source_id)
    assert row is not None
    assert row.status == ReservationStatus.ACTIVE
    assert stock is not None
    assert stock.held == 1


async def test_cancel_internal_reservation_releases_stock_and_finishes_cancelled(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="CANCEL", on_hand=4
    )

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="cancel-create", quantity=2)
        reservation_id = UUID(created.json()["reservation_id"])
        cancelled = await client.post(
            f"/reservations/{reservation_id}/cancel",
            headers=create_headers(user_id="user-1"),
        )

    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "RELEASING"

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        stock = await session.get(InternalStockModel, source.source_id)
    assert reservation is not None
    assert reservation.status == ReservationStatus.RELEASING
    assert reservation.release_reason == "USER_CANCELLED"
    assert stock is not None
    assert stock.held == 2

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
        stock = await session.get(InternalStockModel, source.source_id)
    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED
    assert reservation.release_reason == "USER_CANCELLED"
    assert line is not None
    assert line.status == ReservationLineStatus.RELEASED
    assert stock is not None
    assert stock.on_hand == 4
    assert stock.held == 0


async def test_payment_success_confirms_consumes_stock_and_creates_one_order_with_lines(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="PAY-SUCCESS", on_hand=4
    )
    event_id = uuid4()

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(
            client, source, key="pay-success-create", quantity=2
        )
        reservation_id = UUID(created.json()["reservation_id"])
        response = await client.post(
            f"/reservations/{reservation_id}/payment-outcome",
            headers=create_headers(user_id="user-1"),
            json={"event_id": str(event_id), "outcome": "SUCCESS"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "CONFIRMED"
    order_id = UUID(response.json()["order_id"])

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        stock = await session.get(InternalStockModel, source.source_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )
        order = await session.get(OrderModel, order_id)
        order_lines = (
            await session.scalars(
                select(OrderLineModel).where(OrderLineModel.order_id == order_id)
            )
        ).all()
        payment = await session.get(PaymentEventModel, event_id)

    assert reservation is not None
    assert reservation.status == ReservationStatus.CONFIRMED
    assert reservation.confirmed_at is not None
    assert stock is not None
    assert stock.on_hand == 2
    assert stock.held == 0
    assert line is not None
    assert line.status == ReservationLineStatus.CONFIRMED
    assert order is not None
    assert order.reservation_id == reservation_id
    assert len(order_lines) == 1
    assert order_lines[0].product_id == source.product_id
    assert order_lines[0].stock_source_id == source.source_id
    assert order_lines[0].quantity == 2
    assert payment is not None
    assert payment.outcome == PaymentOutcome.SUCCESS


async def test_duplicate_payment_success_event_is_idempotent_and_does_not_duplicate_order(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="PAY-DUP", on_hand=3
    )
    event_id = uuid4()

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="pay-dup-create")
        reservation_id = UUID(created.json()["reservation_id"])
        payload = {"event_id": str(event_id), "outcome": "SUCCESS"}
        first = await client.post(
            f"/reservations/{reservation_id}/payment-outcome",
            headers=create_headers(user_id="user-1"),
            json=payload,
        )
        second = await client.post(
            f"/reservations/{reservation_id}/payment-outcome",
            headers=create_headers(user_id="user-1"),
            json=payload,
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["order_id"] == second.json()["order_id"]

    async with postgres_session_factory() as session:
        order_count = await session.scalar(
            select(func.count()).select_from(OrderModel)
        )
        line_count = await session.scalar(
            select(func.count()).select_from(OrderLineModel)
        )
        event_count = await session.scalar(
            select(func.count()).select_from(PaymentEventModel)
        )
        stock = await session.get(InternalStockModel, source.source_id)

    assert order_count == 1
    assert line_count == 1
    assert event_count == 1
    assert stock is not None
    assert stock.on_hand == 2
    assert stock.held == 0


async def test_reused_payment_event_id_with_different_outcome_conflicts_without_undoing_order(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="PAY-CONFLICT", on_hand=2
    )
    event_id = uuid4()

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="pay-conflict-create")
        reservation_id = UUID(created.json()["reservation_id"])
        success = await client.post(
            f"/reservations/{reservation_id}/payment-outcome",
            headers=create_headers(user_id="user-1"),
            json={"event_id": str(event_id), "outcome": "SUCCESS"},
        )
        conflict = await client.post(
            f"/reservations/{reservation_id}/payment-outcome",
            headers=create_headers(user_id="user-1"),
            json={"event_id": str(event_id), "outcome": "FAILURE"},
        )

    assert success.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        event = await session.get(PaymentEventModel, event_id)
        order_count = await session.scalar(
            select(func.count()).select_from(OrderModel)
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.CONFIRMED
    assert event is not None
    assert event.outcome == PaymentOutcome.SUCCESS
    assert order_count == 1


async def test_payment_failure_enters_releasing_then_restores_internal_availability(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="PAY-FAIL", on_hand=3
    )
    event_id = uuid4()

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(
            client, source, key="pay-fail-create", quantity=2
        )
        reservation_id = UUID(created.json()["reservation_id"])
        response = await client.post(
            f"/reservations/{reservation_id}/payment-outcome",
            headers=create_headers(user_id="user-1"),
            json={"event_id": str(event_id), "outcome": "FAILURE"},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "RELEASING"

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        payment = await session.get(PaymentEventModel, event_id)
        stock = await session.get(InternalStockModel, source.source_id)
    assert reservation is not None
    assert reservation.release_reason == "PAYMENT_FAILED"
    assert payment is not None
    assert payment.outcome == PaymentOutcome.FAILURE
    assert stock is not None
    assert stock.held == 2

    await ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    ).execute(reservation_id)

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        stock = await session.get(InternalStockModel, source.source_id)
        order_count = await session.scalar(
            select(func.count()).select_from(OrderModel)
        )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED
    assert stock is not None
    assert stock.on_hand == 3
    assert stock.held == 0
    assert order_count == 0


async def test_payment_outcome_requires_matching_owner_and_records_no_event_on_failure(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="PAY-WRONG-OWNER", on_hand=2
    )
    event_id = uuid4()

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="wrong-owner-create")
        reservation_id = UUID(created.json()["reservation_id"])
        response = await client.post(
            f"/reservations/{reservation_id}/payment-outcome",
            headers=create_headers(user_id="other-user"),
            json={"event_id": str(event_id), "outcome": "SUCCESS"},
        )

    assert response.status_code == 404
    assert response.json()["code"] == "RESERVATION_NOT_FOUND"

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        event = await session.get(PaymentEventModel, event_id)
        stock = await session.get(InternalStockModel, source.source_id)
    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert event is None
    assert stock is not None
    assert stock.held == 1


async def test_expiry_finishes_as_expired_and_late_payment_success_cannot_resurrect(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="EXPIRE", on_hand=2
    )
    event_id = uuid4()

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="expire-create")
        reservation_id = UUID(created.json()["reservation_id"])

        async with postgres_session_factory.begin() as session:
            await session.execute(
                update(ReservationModel)
                .where(ReservationModel.id == reservation_id)
                .values(
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=5)
                )
            )

        claimed = await ExpireReservingReservationService(
            uow_factory=uow_factory(postgres_session_factory)
        ).execute_batch(limit=10)
        assert reservation_id in claimed
        await ProcessReleasingReservationService(
            uow_factory=uow_factory(postgres_session_factory)
        ).execute(reservation_id)

        late = await client.post(
            f"/reservations/{reservation_id}/payment-outcome",
            headers=create_headers(user_id="user-1"),
            json={"event_id": str(event_id), "outcome": "SUCCESS"},
        )

    assert late.status_code == 409
    assert late.json()["code"] == "RESERVATION_STATE_CONFLICT"

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        stock = await session.get(InternalStockModel, source.source_id)
        event = await session.get(PaymentEventModel, event_id)
        order_count = await session.scalar(
            select(func.count()).select_from(OrderModel)
        )

    assert reservation is not None
    assert reservation.status == ReservationStatus.EXPIRED
    assert reservation.release_reason == "EXPIRED"
    assert stock is not None
    assert stock.held == 0
    assert stock.on_hand == 2
    assert event is None
    assert order_count == 0


async def test_direct_confirm_admin_endpoint_is_idempotent_and_uses_same_finalization(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="DIRECT-CONFIRM", on_hand=3
    )

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="confirm-create")
        reservation_id = UUID(created.json()["reservation_id"])
        first = await client.post(
            f"/reservations/{reservation_id}/confirm",
            headers=create_headers(user_id="user-1"),
        )
        second = await client.post(
            f"/reservations/{reservation_id}/confirm",
            headers=create_headers(user_id="user-1"),
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == "CONFIRMED"
    assert first.json()["order_id"] == second.json()["order_id"]

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        stock = await session.get(InternalStockModel, source.source_id)
        order_count = await session.scalar(
            select(func.count()).select_from(OrderModel)
        )
        order_line_count = await session.scalar(
            select(func.count()).select_from(OrderLineModel)
        )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONFIRMED
    assert stock is not None
    assert stock.on_hand == 2
    assert stock.held == 0
    assert order_count == 1
    assert order_line_count == 1


async def test_direct_confirm_rejects_wrong_owner_without_database_mutation(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="CONFIRM-WRONG-OWNER", on_hand=3
    )

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="confirm-owner-create")
        reservation_id = UUID(created.json()["reservation_id"])
        response = await client.post(
            f"/reservations/{reservation_id}/confirm",
            headers=create_headers(user_id="other-user"),
        )

    assert response.status_code == 404
    assert response.json()["code"] == "RESERVATION_NOT_FOUND"

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        stock = await session.get(InternalStockModel, source.source_id)
        order_count = await session.scalar(
            select(func.count()).select_from(OrderModel)
        )
    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert stock is not None
    assert stock.held == 1
    assert order_count == 0


async def test_confirmed_reservation_cannot_be_cancelled(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="CANCEL-CONFIRMED", on_hand=2
    )

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="cancel-confirmed-create")
        reservation_id = UUID(created.json()["reservation_id"])
        confirmed = await client.post(
            f"/reservations/{reservation_id}/confirm",
            headers=create_headers(user_id="user-1"),
        )
        assert confirmed.status_code == 200
        cancelled = await client.post(
            f"/reservations/{reservation_id}/cancel",
            headers=create_headers(user_id="user-1"),
        )

    assert cancelled.status_code == 409
    assert cancelled.json()["code"] == "RESERVATION_STATE_CONFLICT"

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        order_count = await session.scalar(
            select(func.count()).select_from(OrderModel)
        )
        stock = await session.get(InternalStockModel, source.source_id)
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONFIRMED
    assert order_count == 1
    assert stock is not None
    assert stock.held == 0
    assert stock.on_hand == 1
