from typing import Callable
from uuid import UUID

from app.application.dto.reservations import (
    PaymentOutcomeCommand,
    PaymentOutcomeResult,
)
from app.application.errors import (
    ReservationNotFound,
    ReservationStateConflict,
)
from app.application.ports.provider_gateway import (
    ProviderGatewayRegistry,
    ProviderRegistry,
)
from app.application.ports.repositories import UnitOfWork
from app.application.services.finalize_reservation import finalize_confirming_reservation
from app.domain.enums import PaymentOutcome, ReservationLineStatus, ReservationStatus

_ATTENTION_STATES = {
    ReservationLineStatus.HOLD_UNKNOWN,
    ReservationLineStatus.RELEASE_UNKNOWN,
    ReservationLineStatus.CONFIRM_UNKNOWN,
}


class ProcessPaymentOutcomeService:
    """Apply a trusted payment result to reservation state.

    Payment processing and payment-event persistence are outside this service.
    Idempotency is state-based: repeated SUCCESS after confirmation returns the
    existing order; repeated FAILURE after release has started returns the
    current reservation snapshot.
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        provider_gateways: ProviderGatewayRegistry | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._provider_gateways = provider_gateways or ProviderRegistry()

    async def execute(self, command: PaymentOutcomeCommand) -> PaymentOutcomeResult:
        async with self._uow_factory() as uow:
            reservation = await uow.reservations.get_by_id(command.reservation_id)
            if reservation is None or reservation.user_id != command.user_id:
                raise ReservationNotFound(f"Reservation {command.reservation_id} was not found.")

            order_id: UUID | None = None

            if command.outcome == PaymentOutcome.SUCCESS:
                if reservation.status == ReservationStatus.CONFIRMED:
                    order_id = await uow.orders.get_by_reservation_id(command.reservation_id)
                    if order_id is None:
                        raise ReservationStateConflict(
                            "Confirmed reservation is missing its order."
                        )
                elif reservation.status == ReservationStatus.ACTIVE:
                    if not await uow.reservations.begin_confirming_if_active(
                        command.reservation_id
                    ):
                        raise ReservationStateConflict(
                            "Payment success lost the race with expiry or another transition."
                        )
                    order_id = await finalize_confirming_reservation(
                        uow,
                        reservation_id=command.reservation_id,
                        user_id=command.user_id,
                        provider_gateways=self._provider_gateways,
                    )
                    await uow.commit()
                else:
                    raise ReservationStateConflict(
                        f"Payment success cannot finalize reservation from {reservation.status}."
                    )
            else:
                if reservation.status in {
                    ReservationStatus.CONFIRMED,
                    ReservationStatus.CONFIRMING,
                }:
                    raise ReservationStateConflict(
                        "Payment failure cannot undo a confirmed or confirming reservation."
                    )

                if reservation.status in {
                    ReservationStatus.RESERVING,
                    ReservationStatus.ACTIVE,
                }:
                    if not await uow.reservations.begin_releasing(
                        command.reservation_id, "PAYMENT_FAILED"
                    ):
                        raise ReservationStateConflict(
                            "Payment failure lost the race with another transition."
                        )
                    await uow.commit()

                # FAILURE is naturally idempotent once release has started or
                # the reservation is already terminal.

        return await self._snapshot(command.reservation_id, command.user_id)

    async def _snapshot(self, reservation_id: UUID, user_id: str) -> PaymentOutcomeResult:
        async with self._uow_factory() as uow:
            reservation = await uow.reservations.get_by_id(reservation_id)
            if reservation is None or reservation.user_id != user_id:
                raise ReservationNotFound(f"Reservation {reservation_id} was not found.")
            lines = await uow.reservations.get_lines(reservation_id)
            order_id = await uow.orders.get_by_reservation_id(reservation_id)
            return PaymentOutcomeResult(
                reservation_id=reservation_id,
                order_id=order_id,
                status=reservation.status,
                created_at=reservation.created_at,
                expires_at=reservation.expires_at,
                payment_allowed=reservation.status == ReservationStatus.ACTIVE,
                requires_attention=any(line.status in _ATTENTION_STATES for line in lines),
                lines=lines,
            )
