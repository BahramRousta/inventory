from typing import Callable
from uuid import UUID

from app.application.dto.reservations import ConfirmReservationResult
from app.application.errors import (
    ReservationExpired,
    ReservationNotFound,
    ReservationStateConflict,
)
from app.application.ports.repositories import UnitOfWork
from app.domain.enums import ProviderKind, ReservationLineStatus, ReservationStatus


class ConfirmReservationService:
    """Confirm an ACTIVE reservation and create exactly one order.

    External HOLD is treated as the upstream reservation guarantee for this
    assignment because no provider-confirm endpoint is defined by the provider
    contract. That assumption is documented in ARCHITECTURE.md.
    """

    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def execute(self, reservation_id: UUID) -> ConfirmReservationResult:
        async with self._uow_factory() as uow:
            reservation = await uow.reservations.get_by_id(reservation_id)
            if reservation is None:
                raise ReservationNotFound(f"Reservation {reservation_id} was not found.")

            if reservation.status == ReservationStatus.CONFIRMED:
                order_id = await uow.orders.get_by_reservation_id(reservation_id)
                if order_id is None:
                    raise ReservationStateConflict(
                        "Confirmed reservation is missing its order."
                    )
                lines = await uow.reservations.get_lines(reservation_id)
                return ConfirmReservationResult(
                    reservation_id=reservation_id,
                    order_id=order_id,
                    status=ReservationStatus.CONFIRMED,
                    expires_at=reservation.expires_at,
                    payment_allowed=False,
                    lines=lines,
                )

            if reservation.status != ReservationStatus.ACTIVE:
                raise ReservationStateConflict(
                    f"Reservation {reservation_id} cannot be confirmed from "
                    f"{reservation.status}."
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

            lines = await uow.reservations.get_lines(reservation_id)
            sources = await uow.stock_sources.get_many(
                tuple(line.stock_source_id for line in lines)
            )

            for line in lines:
                if line.status != ReservationLineStatus.HELD:
                    raise ReservationStateConflict(
                        f"Line {line.stock_source_id} is not held."
                    )
                source = sources.get(line.stock_source_id)
                if source is None:
                    raise ReservationStateConflict(
                        f"Stock source {line.stock_source_id} disappeared."
                    )
                if source.provider_kind == ProviderKind.INTERNAL:
                    consumed = await uow.inventory.consume_hold(
                        line.stock_source_id, line.quantity
                    )
                    if not consumed:
                        raise ReservationStateConflict(
                            f"Internal hold for {line.stock_source_id} cannot be consumed."
                        )
                await uow.reservations.mark_line_confirmed(
                    reservation_id, line.stock_source_id
                )

            if not await uow.reservations.confirm_if_all_lines_confirmed(reservation_id):
                raise ReservationStateConflict(
                    f"Reservation {reservation_id} could not be finalized."
                )

            order_id = await uow.orders.create(
                reservation_id=reservation_id,
                user_id=reservation.user_id,
            )
            await uow.commit()

        async with self._uow_factory() as uow:
            confirmed = await uow.reservations.get_by_id(reservation_id)
            assert confirmed is not None
            lines = await uow.reservations.get_lines(reservation_id)
            return ConfirmReservationResult(
                reservation_id=reservation_id,
                order_id=order_id,
                status=confirmed.status,
                expires_at=confirmed.expires_at,
                payment_allowed=False,
                lines=lines,
            )