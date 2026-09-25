from app.application.services.create_reservation import CreateReservationService
from app.bootstrap.config import get_settings
from app.infrastructure.clock import SystemClock
from app.infrastructure.db.session import AsyncSessionLocal
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork


def get_create_reservation_service() -> CreateReservationService:
    settings = get_settings()
    return CreateReservationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(AsyncSessionLocal),
        clock=SystemClock(),
        ttl_seconds=settings.reservation_ttl_seconds,
    )
