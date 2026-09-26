from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from app.application.services.cancel.process_releasing_reservation import (
    ProcessReleasingReservationService,
)
from app.domain.enums import (
    ReservationLineStatus,
    ReservationStatus,
)
from app.infrastructure.db.models import (
    InternalStockModel,
    OrderModel,
    ReservationLineModel,
    ReservationModel,
)
from tests.conftest import (
    api_client,
    create_body,
    create_headers,
    reservation_count,
    reservation_line_count,
    seed_external_source,
    seed_internal_source,
    seed_product,
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
    wrong_product_id = await seed_product(
        postgres_session_factory,
        sku="WRONG-PRODUCT",
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


async def test_enabled_external_provider_is_accepted_before_provider_processing(
    postgres_session_factory,
):
    source = await seed_external_source(
        postgres_session_factory,
        sku="EXTERNAL-PENDING",
    )

    async with api_client(postgres_session_factory) as client:
        response = await client.post(
            "/reservations",
            headers=create_headers(idempotency_key="external-pending"),
            json=create_body(source),
        )

    assert response.status_code == 202
    reservation_id = UUID(response.json()["reservation_id"])

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
    assert line.status == ReservationLineStatus.HOLD_PENDING


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

async def test_same_idempotency_key_with_different_request_returns_conflict_without_mutation(
    postgres_session_factory,
):
    """Scenario: an idempotency key is reused with a different request body.

    Given an existing reservation created with quantity two,
    When the same user reuses the same Idempotency-Key with quantity one,
    Then the API returns IDEMPOTENCY_CONFLICT and PostgreSQL keeps only the
    original reservation, line quantity, fingerprint, and held inventory.
    """
    # Given
    source = await seed_internal_source(
        postgres_session_factory,
        sku="IDEMPOTENCY-FINGERPRINT",
        on_hand=5,
    )

    async with api_client(postgres_session_factory) as client:
        first = await _create_internal(
            client,
            source,
            key="fingerprint-key",
            quantity=2,
        )

        # When
        second = await _create_internal(
            client,
            source,
            key="fingerprint-key",
            quantity=1,
        )

    # Then
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == "IDEMPOTENCY_CONFLICT"

    reservation_id = UUID(first.json()["reservation_id"])
    async with postgres_session_factory() as session:
        reservation = await session.get(ReservationModel, reservation_id)
        line = await session.scalar(
            select(ReservationLineModel).where(
                ReservationLineModel.reservation_id == reservation_id
            )
        )
        stock = await session.get(InternalStockModel, source.source_id)
        reservations = await session.scalar(
            select(func.count()).select_from(ReservationModel)
        )

    assert reservation is not None
    assert len(reservation.request_fingerprint) == 64
    assert reservation.request_fingerprint != "0" * 64
    assert line is not None
    assert line.quantity == 2
    assert stock is not None
    assert stock.on_hand == 5
    assert stock.held == 2
    assert reservations == 1
