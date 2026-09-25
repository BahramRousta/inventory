from typing import Callable
from uuid import UUID

from app.application.dto.reservations import CreateReservationResult
from app.application.errors import ReservationNotFound, ReservationStateConflict
from app.application.ports.repositories import UnitOfWork
from app.domain.enums import ReservationStatus


class CancelReservationService:
    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def execute(
        self, reservation_id: UUID, *, reason: str = "USER_CANCELLED"
    ) -> CreateReservationResult:
        async with self._uow_factory() as uow:
            reservation = await uow.reservations.get_by_id(reservation_id)
            if reservation is None:
                raise ReservationNotFound(f"Reservation {reservation_id} was not found.")

            if reservation.status == ReservationStatus.CONFIRMED:
                raise ReservationStateConflict(
                    "A confirmed reservation already produced an order and cannot be cancelled."
                )

            if reservation.status not in {
                ReservationStatus.CANCELLED,
                ReservationStatus.RELEASING,
            }:
                changed = await uow.reservations.begin_releasing(
                    reservation_id, reason
                )
                if not changed:
                    raise ReservationStateConflict(
                        f"Reservation {reservation_id} cannot be cancelled from "
                        f"{reservation.status}."
                    )
                await uow.commit()

        async with self._uow_factory() as uow:
            reservation = await uow.reservations.get_by_id(reservation_id)
            assert reservation is not None
            lines = await uow.reservations.get_lines(reservation_id)
            return CreateReservationResult(
                reservation_id=reservation.reservation_id,
                status=reservation.status,
                expires_at=reservation.expires_at,
                payment_allowed=False,
                lines=lines,
            )