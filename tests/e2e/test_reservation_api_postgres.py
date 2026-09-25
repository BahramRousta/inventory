from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from app.infrastructure.providers.mock import MockAvailabilityProviderGateway
from app.application.services.expire_reserving_reservation import (
    ExpireReservingReservationService,
)
from app.application.services.process_releasing_reservation import (
    ProcessReleasingReservationService,
)
from app.domain.enums import (
    ReservationLineStatus,
    ReservationStatus,
)
from app.infrastructure.db.models import (
    InternalStockModel,
    OrderModel,
    ProductModel,
    ReservationLineModel,
    ReservationModel,
)
from tests.e2e.support import (
    api_client,
    create_body,
    create_headers,
    reservation_count,
    reservation_line_count,
    availability_only_registry,
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
    source = await seed_internal_source(postgres_session_factory, sku="CREATE-OK", on_hand=5)

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
    source = await seed_internal_source(postgres_session_factory, sku="DUP-LINES", on_hand=10)
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
    source = await seed_internal_source(postgres_session_factory, sku="IDEMPOTENT", on_hand=5)

    async with api_client(postgres_session_factory) as client:
        first = await _create_internal(client, source, key="same-create-key", quantity=2)
        second = await _create_internal(client, source, key="same-create-key", quantity=2)

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["reservation_id"] == second.json()["reservation_id"]

    async with postgres_session_factory() as session:
        stock = await session.get(InternalStockModel, source.source_id)
        reservations = await session.scalar(select(func.count()).select_from(ReservationModel))

    assert reservations == 1
    assert stock is not None
    assert stock.held == 2


async def test_idempotency_key_with_changed_body_returns_conflict_without_mutation(
    postgres_session_factory,
):
    source = await seed_internal_source(postgres_session_factory, sku="IDEM-CONFLICT", on_hand=5)

    async with api_client(postgres_session_factory) as client:
        first = await _create_internal(client, source, key="body-key", quantity=1)
        conflict = await _create_internal(client, source, key="body-key", quantity=2)

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
    source = await seed_internal_source(postgres_session_factory, sku="NO-STOCK", on_hand=1)

    async with api_client(postgres_session_factory) as client:
        response = await _create_internal(client, source, key="too-many", quantity=2)

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
    source = await seed_internal_source(postgres_session_factory, sku="MISMATCH", on_hand=5)
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


async def test_query_only_provider_is_rejected_for_reservation_workflow(
    postgres_session_factory,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="QUERY-ONLY",
    )
    registry = availability_only_registry(
        source.provider_id,
        MockAvailabilityProviderGateway(),
    )

    async with api_client(
        postgres_session_factory,
        provider_gateways=registry,
    ) as client:
        response = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="unsupported-external"),
            json=create_body(source),
        )

    assert response.status_code == 422
    assert response.json()["code"] == "SOURCE_NOT_RESERVABLE"
    assert await reservation_count(postgres_session_factory) == 0

    async with postgres_session_factory() as session:
        lines = await session.scalar(select(func.count()).select_from(ReservationLineModel))
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
        count = await session.scalar(select(func.count()).select_from(ReservationLineModel))
    assert count == 0


async def test_get_reservation_returns_persisted_snapshot_for_owner(
    postgres_session_factory,
):
    source = await seed_internal_source(postgres_session_factory, sku="GET-OWNER", on_hand=3)

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
    source = await seed_internal_source(postgres_session_factory, sku="GET-WRONG-OWNER", on_hand=3)

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
    source = await seed_internal_source(postgres_session_factory, sku="CANCEL", on_hand=4)

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="cancel-create", quantity=2)
        reservation_id = UUID(created.json()["reservation_id"])
        cancelled = await client.post(
            f"/reservations/{reservation_id}/cancel",
            headers=create_headers(user_id="user-1"),
        )

    assert cancelled.status_code == 202
    assert cancelled.headers["retry-after"] == "1"
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
    source = await seed_internal_source(postgres_session_factory, sku="PAY-SUCCESS", on_hand=4)
    event_id = uuid4()

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="pay-success-create", quantity=2)
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


async def test_duplicate_payment_success_event_is_idempotent_and_does_not_duplicate_order(
    postgres_session_factory,
):
    source = await seed_internal_source(postgres_session_factory, sku="PAY-DUP", on_hand=3)
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
        order_count = await session.scalar(select(func.count()).select_from(OrderModel))
        stock = await session.get(InternalStockModel, source.source_id)

    assert order_count == 1
    assert stock is not None
    assert stock.on_hand == 2
    assert stock.held == 0


async def test_payment_failure_enters_releasing_then_restores_internal_availability(
    postgres_session_factory,
):
    source = await seed_internal_source(postgres_session_factory, sku="PAY-FAIL", on_hand=3)
    event_id = uuid4()

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="pay-fail-create", quantity=2)
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
        stock = await session.get(InternalStockModel, source.source_id)
    assert reservation is not None
    assert reservation.release_reason == "PAYMENT_FAILED"
    assert stock is not None
    assert stock.held == 2

    await ProcessReleasingReservationService(
        uow_factory=uow_factory(postgres_session_factory)
    ).execute(reservation_id)

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        stock = await session.get(InternalStockModel, source.source_id)
        order_count = await session.scalar(select(func.count()).select_from(OrderModel))
    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED
    assert stock is not None
    assert stock.on_hand == 3
    assert stock.held == 0
    assert order_count == 0


async def test_payment_outcome_requires_matching_owner_without_state_change(
    postgres_session_factory,
):
    source = await seed_internal_source(postgres_session_factory, sku="PAY-WRONG-OWNER", on_hand=2)
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
        stock = await session.get(InternalStockModel, source.source_id)
    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert stock is not None
    assert stock.held == 1


async def test_expiry_finishes_as_expired_and_late_payment_success_cannot_resurrect(
    postgres_session_factory,
):
    source = await seed_internal_source(postgres_session_factory, sku="EXPIRE", on_hand=2)
    event_id = uuid4()

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="expire-create")
        reservation_id = UUID(created.json()["reservation_id"])

        async with postgres_session_factory.begin() as session:
            await session.execute(
                update(ReservationModel)
                .where(ReservationModel.id == reservation_id)
                .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=5))
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
        order_count = await session.scalar(select(func.count()).select_from(OrderModel))

    assert reservation is not None
    assert reservation.status == ReservationStatus.EXPIRED
    assert reservation.release_reason == "EXPIRED"
    assert stock is not None
    assert stock.held == 0
    assert stock.on_hand == 2
    assert order_count == 0


async def test_direct_confirm_admin_endpoint_is_idempotent_and_uses_same_finalization(
    postgres_session_factory,
):
    source = await seed_internal_source(postgres_session_factory, sku="DIRECT-CONFIRM", on_hand=3)

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
        order_count = await session.scalar(select(func.count()).select_from(OrderModel))
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONFIRMED
    assert stock is not None
    assert stock.on_hand == 2
    assert stock.held == 0
    assert order_count == 1


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
        order_count = await session.scalar(select(func.count()).select_from(OrderModel))
    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert stock is not None
    assert stock.held == 1
    assert order_count == 0


async def test_confirmed_reservation_cannot_be_cancelled(
    postgres_session_factory,
):
    source = await seed_internal_source(postgres_session_factory, sku="CANCEL-CONFIRMED", on_hand=2)

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
        order_count = await session.scalar(select(func.count()).select_from(OrderModel))
        stock = await session.get(InternalStockModel, source.source_id)
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONFIRMED
    assert order_count == 1
    assert stock is not None
    assert stock.held == 0
    assert stock.on_hand == 1


async def test_invalid_quantity_validation_creates_no_database_state(
    postgres_session_factory,
):
    source = await seed_internal_source(postgres_session_factory, sku="INVALID-QTY", on_hand=5)

    async with api_client(postgres_session_factory) as client:
        response = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="invalid-qty"),
            json={
                "items": [
                    {
                        "product_id": str(source.product_id),
                        "stock_source_id": str(source.source_id),
                        "quantity": 0,
                    }
                ]
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert await reservation_count(postgres_session_factory) == 0

    async with postgres_session_factory() as session:
        stock = await session.get(InternalStockModel, source.source_id)
    assert stock is not None
    assert stock.held == 0


async def test_duplicate_line_total_overflow_is_rejected_before_stock_mutation(
    postgres_session_factory,
):
    source = await seed_internal_source(postgres_session_factory, sku="OVERFLOW-QTY", on_hand=10)
    max_quantity = 2_147_483_647

    async with api_client(postgres_session_factory) as client:
        response = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="overflow-qty"),
            json={
                "items": [
                    {
                        "product_id": str(source.product_id),
                        "stock_source_id": str(source.source_id),
                        "quantity": max_quantity,
                    },
                    {
                        "product_id": str(source.product_id),
                        "stock_source_id": str(source.source_id),
                        "quantity": 1,
                    },
                ]
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_RESERVATION_ITEMS"
    assert await reservation_count(postgres_session_factory) == 0

    async with postgres_session_factory() as session:
        stock = await session.get(InternalStockModel, source.source_id)
    assert stock is not None
    assert stock.held == 0


async def test_get_unknown_reservation_returns_404_and_database_remains_empty(
    postgres_session_factory,
):
    missing_id = uuid4()

    async with api_client(postgres_session_factory) as client:
        response = await client.get(
            f"/reservations/{missing_id}",
            headers=create_headers(user_id="user-1"),
        )

    assert response.status_code == 404
    assert response.json()["code"] == "RESERVATION_NOT_FOUND"
    assert await reservation_count(postgres_session_factory) == 0

    async with postgres_session_factory() as session:
        line_count = await session.scalar(select(func.count()).select_from(ReservationLineModel))
    assert line_count == 0


async def test_cancel_rejects_wrong_owner_without_changing_hold(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="CANCEL-WRONG-OWNER", on_hand=2
    )

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="cancel-wrong-owner")
        reservation_id = UUID(created.json()["reservation_id"])
        response = await client.post(
            f"/reservations/{reservation_id}/cancel",
            headers=create_headers(user_id="other-user"),
        )

    assert response.status_code == 404
    assert response.json()["code"] == "RESERVATION_NOT_FOUND"

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        stock = await session.get(InternalStockModel, source.source_id)
    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert stock is not None
    assert stock.held == 1


async def test_repeated_cancel_does_not_release_internal_stock_twice(
    postgres_session_factory,
):
    source = await seed_internal_source(postgres_session_factory, sku="CANCEL-REPLAY", on_hand=2)

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="cancel-replay-create")
        reservation_id = UUID(created.json()["reservation_id"])
        first = await client.post(
            f"/reservations/{reservation_id}/cancel",
            headers=create_headers(user_id="user-1"),
        )
        second = await client.post(
            f"/reservations/{reservation_id}/cancel",
            headers=create_headers(user_id="user-1"),
        )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["status"] == "RELEASING"
    assert second.json()["status"] == "RELEASING"

    service = ProcessReleasingReservationService(uow_factory=uow_factory(postgres_session_factory))
    await service.execute(reservation_id)
    await service.execute(reservation_id)

    async with api_client(postgres_session_factory) as client:
        settled_replay = await client.post(
            f"/reservations/{reservation_id}/cancel",
            headers=create_headers(user_id="user-1"),
        )
    assert settled_replay.status_code == 200
    assert settled_replay.json()["status"] == "CANCELLED"

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        stock = await session.get(InternalStockModel, source.source_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CANCELLED
    assert stock is not None
    assert stock.on_hand == 2
    assert stock.held == 0
    assert line is not None
    assert line.status == ReservationLineStatus.RELEASED


async def test_new_failure_event_after_success_is_rejected_as_contradictory(
    postgres_session_factory,
):
    source = await seed_internal_source(postgres_session_factory, sku="PAY-LATE-FAILURE", on_hand=2)

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="late-failure-create")
        reservation_id = UUID(created.json()["reservation_id"])
        success_event = uuid4()
        failure_event = uuid4()
        success = await client.post(
            f"/reservations/{reservation_id}/payment-outcome",
            headers=create_headers(user_id="user-1"),
            json={"event_id": str(success_event), "outcome": "SUCCESS"},
        )
        failure = await client.post(
            f"/reservations/{reservation_id}/payment-outcome",
            headers=create_headers(user_id="user-1"),
            json={"event_id": str(failure_event), "outcome": "FAILURE"},
        )

    assert success.status_code == 200
    assert failure.status_code == 409
    assert failure.json()["code"] == "RESERVATION_STATE_CONFLICT"

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        order_count = await session.scalar(select(func.count()).select_from(OrderModel))
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONFIRMED
    assert order_count == 1


async def test_direct_confirm_after_expiry_is_rejected_and_creates_no_order(
    postgres_session_factory,
):
    source = await seed_internal_source(postgres_session_factory, sku="CONFIRM-EXPIRED", on_hand=2)

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="confirm-expired-create")
        reservation_id = UUID(created.json()["reservation_id"])

        async with postgres_session_factory.begin() as session:
            await session.execute(
                update(ReservationModel)
                .where(ReservationModel.id == reservation_id)
                .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )

        response = await client.post(
            f"/reservations/{reservation_id}/confirm",
            headers=create_headers(user_id="user-1"),
        )

    assert response.status_code == 409
    assert response.json()["code"] == "RESERVATION_EXPIRED"

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        stock = await session.get(InternalStockModel, source.source_id)
        order_count = await session.scalar(select(func.count()).select_from(OrderModel))
    assert reservation is not None
    assert reservation.status == ReservationStatus.ACTIVE
    assert stock is not None
    assert stock.held == 1
    assert order_count == 0


async def test_multi_item_payment_success_confirms_all_reservation_lines_and_creates_one_order(
    postgres_session_factory,
):
    first_source = await seed_internal_source(
        postgres_session_factory, sku="MULTI-ORDER-A", on_hand=3
    )
    second_source = await seed_internal_source(
        postgres_session_factory, sku="MULTI-ORDER-B", on_hand=4
    )
    event_id = uuid4()

    body = {
        "items": [
            {
                "product_id": str(first_source.product_id),
                "stock_source_id": str(first_source.source_id),
                "quantity": 2,
            },
            {
                "product_id": str(second_source.product_id),
                "stock_source_id": str(second_source.source_id),
                "quantity": 1,
            },
        ]
    }

    async with api_client(postgres_session_factory) as client:
        created = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="multi-order-create"),
            json=body,
        )
        assert created.status_code == 201
        reservation_id = UUID(created.json()["reservation_id"])
        paid = await client.post(
            f"/reservations/{reservation_id}/payment-outcome",
            headers=create_headers(user_id="user-1"),
            json={"event_id": str(event_id), "outcome": "SUCCESS"},
        )

    assert paid.status_code == 200
    assert paid.json()["status"] == "CONFIRMED"

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        reservation_lines = (
            await session.scalars(
                select(ReservationLineModel).where(
                    ReservationLineModel.reservation_id == reservation_id
                )
            )
        ).all()
        first_stock = await session.get(InternalStockModel, first_source.source_id)
        second_stock = await session.get(InternalStockModel, second_source.source_id)
        order_count = await session.scalar(select(func.count()).select_from(OrderModel))

    assert reservation is not None
    assert reservation.status == ReservationStatus.CONFIRMED
    assert len(reservation_lines) == 2
    assert all(line.status == ReservationLineStatus.CONFIRMED for line in reservation_lines)
    assert order_count == 1
    assert first_stock is not None
    assert first_stock.on_hand == 1
    assert first_stock.held == 0
    assert second_stock is not None
    assert second_stock.on_hand == 3
    assert second_stock.held == 0


async def test_idempotency_fingerprint_uses_canonicalized_duplicate_lines(
    postgres_session_factory,
):
    source = await seed_internal_source(
        postgres_session_factory, sku="CANONICAL-IDEMPOTENCY", on_hand=5
    )
    duplicate_body = {
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
    canonical_body = create_body(source, quantity=3)

    async with api_client(postgres_session_factory) as client:
        first = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="canonical-key"),
            json=duplicate_body,
        )
        replay = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="canonical-key"),
            json=canonical_body,
        )

    assert first.status_code == 201
    assert replay.status_code == 200
    assert first.json()["reservation_id"] == replay.json()["reservation_id"]

    reservation_id = UUID(first.json()["reservation_id"])
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )
        stock = await session.get(InternalStockModel, source.source_id)

    assert reservation is not None
    assert line is not None
    assert line.quantity == 3
    assert stock is not None
    assert stock.held == 3


async def test_repeated_payment_success_still_requires_reservation_owner(
    postgres_session_factory,
):
    source = await seed_internal_source(postgres_session_factory, sku="PAY-REPLAY-OWNER", on_hand=2)
    event_id = uuid4()

    async with api_client(postgres_session_factory) as client:
        created = await _create_internal(client, source, key="pay-replay-owner-create")
        reservation_id = UUID(created.json()["reservation_id"])
        success = await client.post(
            f"/reservations/{reservation_id}/payment-outcome",
            headers=create_headers(user_id="user-1"),
            json={"event_id": str(event_id), "outcome": "SUCCESS"},
        )
        wrong_owner_replay = await client.post(
            f"/reservations/{reservation_id}/payment-outcome",
            headers=create_headers(user_id="other-user"),
            json={"event_id": str(event_id), "outcome": "SUCCESS"},
        )

    assert success.status_code == 200
    assert wrong_owner_replay.status_code == 404
    assert wrong_owner_replay.json()["code"] == "RESERVATION_NOT_FOUND"

    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        order_count = await session.scalar(select(func.count()).select_from(OrderModel))
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONFIRMED
    assert order_count == 1
