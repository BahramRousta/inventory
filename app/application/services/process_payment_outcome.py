import hashlib
import json
from typing import Callable
from uuid import UUID

from app.application.dto.reservations import (
    PaymentEventRecord,
    PaymentOutcomeCommand,
    PaymentOutcomeResult,
)
from app.application.errors import (
    IdempotencyConflict,
    PersistenceConflict,
    ReservationNotFound,
    ReservationStateConflict,
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
    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def execute(self, command: PaymentOutcomeCommand) -> PaymentOutcomeResult:
        payload_hash = _payload_hash(command)
        try:
            return await self._execute(command, payload_hash)
        except PersistenceConflict:
            async with self._uow_factory() as uow:
                existing = await uow.payment_events.get(command.event_id)
                if existing is None:
                    raise
                _assert_same_event(existing, payload_hash)
            return await self._snapshot(command.reservation_id, command.user_id)

    async def _execute(
        self, command: PaymentOutcomeCommand, payload_hash: str
    ) -> PaymentOutcomeResult:
        async with self._uow_factory() as uow:
            existing = await uow.payment_events.get(command.event_id)
            if existing is not None:
                _assert_same_event(existing, payload_hash)
                return await _snapshot_from_uow(
                    uow, command.reservation_id, command.user_id
                )

            reservation = await uow.reservations.get_by_id(command.reservation_id)
            if reservation is None or reservation.user_id != command.user_id:
                raise ReservationNotFound(
                    f"Reservation {command.reservation_id} was not found."
                )

            order_id: UUID | None = None
            if command.outcome == PaymentOutcome.SUCCESS:
                if reservation.status == ReservationStatus.CONFIRMED:
                    order_id = await uow.orders.get_by_reservation_id(
                        command.reservation_id
                    )
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
                    )
                else:
                    raise ReservationStateConflict(
                        f"Payment success cannot finalize reservation from "
                        f"{reservation.status}."
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

            await uow.payment_events.create(
                PaymentEventRecord(
                    event_id=command.event_id,
                    reservation_id=command.reservation_id,
                    user_id=command.user_id,
                    outcome=command.outcome,
                    payload_hash=payload_hash,
                )
            )
            await uow.commit()

        return await self._snapshot(command.reservation_id, command.user_id)

    async def _snapshot(
        self, reservation_id: UUID, user_id: str
    ) -> PaymentOutcomeResult:
        async with self._uow_factory() as uow:
            return await _snapshot_from_uow(uow, reservation_id, user_id)


async def _snapshot_from_uow(
    uow: UnitOfWork, reservation_id: UUID, user_id: str
) -> PaymentOutcomeResult:
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


def _payload_hash(command: PaymentOutcomeCommand) -> str:
    payload = json.dumps(
        {
            "reservation_id": str(command.reservation_id),
            "user_id": command.user_id,
            "outcome": command.outcome.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _assert_same_event(existing: PaymentEventRecord, payload_hash: str) -> None:
    if existing.payload_hash != payload_hash:
        raise IdempotencyConflict(
            "Payment event ID was already used with different content."
        )
