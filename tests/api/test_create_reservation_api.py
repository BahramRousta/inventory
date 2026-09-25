from datetime import datetime, timezone

from httpx import ASGITransport, AsyncClient

from app.application.services.create.create_reservation import CreateReservationService
from app.bootstrap.dependencies import get_create_reservation_service
from app.main import app


class FixedClock:
    def now(self):
        return datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


async def test_post_reservations_returns_201(uow_factory, seed_internal_source):
    product_id, source_id = seed_internal_source
    service = CreateReservationService(
        uow_factory=uow_factory,
        clock=FixedClock(),
        ttl_seconds=900,
    )
    app.dependency_overrides[get_create_reservation_service] = lambda: service
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/reservations",
                headers={
                    "Idempotency-Key": "api-create-1",
                    "X-User-Id": "user-1",
                },
                json={
                    "items": [
                        {
                            "product_id": str(product_id),
                            "stock_source_id": str(source_id),
                            "quantity": 1,
                        }
                    ]
                },
            )
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "ACTIVE"
        assert body["payment_allowed"] is True
        assert body["requires_attention"] is False
        assert body["created_at"]
    finally:
        app.dependency_overrides.clear()
