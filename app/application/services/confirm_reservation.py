from typing import Callable
from uuid import UUID

from app.application.dto.reservations import ConfirmReservationResult
from app.application.errors import (
    ReservationExpired,
    ReservationNotFound,
    ReservationStateConflict,
)
from app.application.ports.repositories import UnitOfWork
from app.domain.enums import ReservationLineStatus, ReservationStatus, ProviderKind

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
    ) -> None:
        self._uow_factory = uow_factory

    async def execute(self, reservation_id: UUID, *, user_id: str) -> ConfirmReservationResult:
        async with self._uow_factory() as uow:
            reservation = await uow.reservations.get_by_id(reservation_id)
            if reservation is None or reservation.user_id != user_id:
                raise ReservationNotFound(f"Reservation {reservation_id} was not found.")

            # it's already confirmed
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

            # the reservation expired already
            if not await uow.reservations.begin_confirming_if_active(reservation_id):
                latest = await uow.reservations.get_by_id(reservation_id)
                if latest is not None and latest.status == ReservationStatus.ACTIVE:
                    raise ReservationExpired(
                        f"Reservation {reservation_id} expired before confirmation."
                    )
                raise ReservationStateConflict(
                    f"Reservation {reservation_id} could not enter CONFIRMING."
                )

            order_id = await self.finalize_confirming_reservation(
                uow,
                reservation_id=reservation_id,
                user_id=user_id,
            )
            await uow.commit()

        async with self._uow_factory() as uow:
            confirmed = await uow.reservations.get_by_id(reservation_id)
            assert confirmed is not None
            lines = await uow.reservations.get_lines(reservation_id)
            return _result(confirmed, order_id, lines)

    async def finalize_confirming_reservation(
        self,
        uow: UnitOfWork,
        *,
        reservation_id: UUID,
        user_id: str,
    ) -> UUID:
        lines = await uow.reservations.get_lines(reservation_id)
        if not lines:
            raise ReservationStateConflict("Reservation has no lines.")

        sources = await uow.stock_sources.get_many(tuple(line.stock_source_id for line in lines))

        for line in lines:
            # the reservation line is not ready for confirmed.
            if line.status != ReservationLineStatus.HELD:
                raise ReservationStateConflict(f"Line {line.stock_source_id} is not held.")

            source = sources.get(line.stock_source_id)
            if source is None:
                raise ReservationStateConflict(f"Stock source {line.stock_source_id} disappeared.")

            # finalize local db state
            if source.provider_kind == ProviderKind.INTERNAL:
                consumed = await uow.inventory.consume_hold(line.stock_source_id, line.quantity)
                if not consumed:
                    raise ReservationStateConflict(
                        f"Internal hold for {line.stock_source_id} cannot be consumed."
                    )

            await uow.reservations.mark_line_confirmed(reservation_id, line.stock_source_id)

        if not await uow.reservations.confirm_if_all_lines_confirmed(reservation_id):
            raise ReservationStateConflict(f"Reservation {reservation_id} could not be finalized.")

        return await uow.orders.create(
            reservation_id=reservation_id,
            user_id=user_id,
        )


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
