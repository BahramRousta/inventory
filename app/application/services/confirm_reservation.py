from typing import Callable
from uuid import UUID

from app.application.dto.reservations import ConfirmReservationResult
from app.application.errors import (
    ReservationExpired,
    ReservationNotFound,
    ReservationStateConflict,
)
from app.application.ports.provider_gateway import (
    ProviderGatewayRegistry,
    ProviderRegistry,
)
from app.application.ports.repositories import UnitOfWork
from app.application.services.finalize_reservation import finalize_confirming_reservation
from app.domain.enums import ReservationLineStatus, ReservationStatus

_ATTENTION_STATES = {
    ReservationLineStatus.HOLD_UNKNOWN,
    ReservationLineStatus.RELEASE_UNKNOWN,
    ReservationLineStatus.CONFIRM_UNKNOWN,
}


class ConfirmReservationService:
    """Administrative compatibility flow.

    Checkout should normally submit a trusted payment outcome. This service
    remains for administrative/manual use and delegates finalization to the
    same application finalizer used by payment-success handling.
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        provider_gateways: ProviderGatewayRegistry | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._provider_gateways = provider_gateways or ProviderRegistry()

    async def execute(self, reservation_id: UUID, *, user_id: str) -> ConfirmReservationResult:
        async with self._uow_factory() as uow:
            reservation = await uow.reservations.get_by_id(reservation_id)
            if reservation is None or reservation.user_id != user_id:
                raise ReservationNotFound(f"Reservation {reservation_id} was not found.")

            if reservation.status == ReservationStatus.CONFIRMED:
                order_id = await uow.orders.get_by_reservation_id(reservation_id)
                if order_id is None:
                    raise ReservationStateConflict("Confirmed reservation is missing its order.")
                lines = await uow.reservations.get_lines(reservation_id)
                return _result(reservation, order_id, lines)

            if reservation.status != ReservationStatus.ACTIVE:
                raise ReservationStateConflict(
                    f"Reservation {reservation_id} cannot be confirmed from {reservation.status}."
                )

            if not await uow.reservations.begin_confirming_if_active(reservation_id):
                latest = await uow.reservations.get_by_id(reservation_id)
                if latest is not None and latest.status == ReservationStatus.ACTIVE:
                    raise ReservationExpired(
                        f"Reservation {reservation_id} expired before confirmation."
                    )
                raise ReservationStateConflict(
                    f"Reservation {reservation_id} could not enter CONFIRMING."
                )

            order_id = await finalize_confirming_reservation(
                uow,
                reservation_id=reservation_id,
                user_id=user_id,
                provider_gateways=self._provider_gateways,
            )
            await uow.commit()

        async with self._uow_factory() as uow:
            confirmed = await uow.reservations.get_by_id(reservation_id)
            assert confirmed is not None
            lines = await uow.reservations.get_lines(reservation_id)
            return _result(confirmed, order_id, lines)


def _result(reservation, order_id, lines) -> ConfirmReservationResult:
    return ConfirmReservationResult(
        reservation_id=reservation.reservation_id,
        order_id=order_id,
        status=reservation.status,
        created_at=reservation.created_at,
        expires_at=reservation.expires_at,
        payment_allowed=False,
        requires_attention=any(line.status in _ATTENTION_STATES for line in lines),
        lines=lines,
    )
