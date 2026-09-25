from typing import Callable
from uuid import UUID

from app.application.dto.reservations import CreateReservationResult
from app.application.errors import ReservationNotFound
from app.application.ports.repositories import UnitOfWork
from app.domain.enums import ReservationStatus


class GetReservationService:
    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def execute(self, reservation_id: UUID) -> CreateReservationResult:
        async with self._uow_factory() as uow:
            reservation = await uow.reservations.get_by_id(reservation_id)
            if reservation is None:
                raise ReservationNotFound(f"Reservation {reservation_id} was not found.")
            lines = await uow.reservations.get_lines(reservation_id)
            return CreateReservationResult(
                reservation_id=reservation.reservation_id,
                status=reservation.status,
                expires_at=reservation.expires_at,
                payment_allowed=reservation.status == ReservationStatus.ACTIVE,
                lines=lines,
            )
