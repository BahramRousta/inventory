"""
Create Reservation
"""

from datetime import timedelta
from hashlib import sha256
import json
from typing import Callable
from uuid import UUID, uuid4

from app.application.dto.reservations import (
    CreateReservationCommand,
    CreateReservationResult,
)
from app.application.errors import (
    IdempotencyConflict,
    InsufficientStock,
    PersistenceConflict,
    ProductSourceMismatch,
    SourceDisabled,
)
from app.application.ports.clock import Clock
from app.application.ports.repositories import UnitOfWork
from app.domain.enums import ProviderKind, ReservationLineStatus, ReservationStatus

_MAX_QUANTITY = 2_147_483_647
_ATTENTION_STATES = {
    ReservationLineStatus.HOLD_UNKNOWN,
    ReservationLineStatus.RELEASE_UNKNOWN,
    ReservationLineStatus.CONFIRM_UNKNOWN,
}


class CreateReservationService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        ttl_seconds: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ttl_seconds = ttl_seconds

    async def execute(self, command: CreateReservationCommand) -> CreateReservationResult:
        items = command.items
        request_fingerprint = _request_fingerprint(items)
        try:
            async with self._uow_factory() as uow:
                existing = await uow.reservations.get_by_idempotency_key(
                    command.user_id, command.idempotency_key
                )
                if existing is not None:
                    _ensure_same_request(existing.request_fingerprint, request_fingerprint)
                    lines = await uow.reservations.get_lines(existing.reservation_id)
                    return _result(existing, lines, replayed=True)

                sources = await uow.stock_sources.get_many(
                    tuple(item.stock_source_id for item in items)
                )
                self._validate_sources(items, sources)

                reservation_id = uuid4()
                now = self._clock.now()
                expires_at = now + timedelta(seconds=self._ttl_seconds)

                await uow.reservations.create(
                    reservation_id=reservation_id,
                    user_id=command.user_id,
                    idempotency_key=command.idempotency_key,
                    request_fingerprint=request_fingerprint,
                    expires_at=expires_at,
                    status=ReservationStatus.RESERVING,
                )

                has_external_lines = False
                for item in items:
                    source = sources[item.stock_source_id]
                    if source.provider_kind == ProviderKind.INTERNAL:
                        if not await uow.inventory.try_hold(item.stock_source_id, item.quantity):
                            raise InsufficientStock(
                                f"Insufficient stock for source {item.stock_source_id}."
                            )
                        await uow.reservations.add_line(
                            reservation_id, item, ReservationLineStatus.HELD
                        )
                    else:
                        has_external_lines = True
                        await uow.reservations.add_line(
                            reservation_id, item, ReservationLineStatus.HOLD_PENDING
                        )

                if not has_external_lines:
                    await uow.reservations.set_status(reservation_id, ReservationStatus.ACTIVE)
                await uow.commit()

            return await self._load_result(reservation_id, replayed=False)

        except PersistenceConflict:
            async with self._uow_factory() as uow:
                existing = await uow.reservations.get_by_idempotency_key(
                    command.user_id, command.idempotency_key
                )
                if existing is None:
                    raise
                _ensure_same_request(existing.request_fingerprint, request_fingerprint)
                lines = await uow.reservations.get_lines(existing.reservation_id)
                return _result(existing, lines, replayed=True)

    async def _load_result(
        self, reservation_id: UUID, *, replayed: bool
    ) -> CreateReservationResult:
        async with self._uow_factory() as uow:
            reservation = await uow.reservations.get_by_id(reservation_id)
            assert reservation is not None
            lines = await uow.reservations.get_lines(reservation_id)
            return _result(reservation, lines, replayed=replayed)

    def _validate_sources(self, items, sources) -> None:
        for item in items:
            source = sources.get(item.stock_source_id)
            if source is None or source.product_id != item.product_id:
                raise ProductSourceMismatch(
                    f"Source {item.stock_source_id} does not belong to product {item.product_id}."
                )
            if not source.source_enabled or not source.provider_enabled:
                raise SourceDisabled(f"Source {item.stock_source_id} is disabled.")


def _result(reservation, lines, *, replayed: bool) -> CreateReservationResult:
    return CreateReservationResult(
        reservation_id=reservation.reservation_id,
        status=reservation.status,
        created_at=reservation.created_at,
        expires_at=reservation.expires_at,
        payment_allowed=reservation.status == ReservationStatus.ACTIVE,
        requires_attention=any(line.status in _ATTENTION_STATES for line in lines),
        lines=lines,
        replayed=replayed,
    )


def _request_fingerprint(items) -> str:
    canonical_items = sorted(
        (
            str(item.product_id),
            str(item.stock_source_id),
            item.quantity,
        )
        for item in items
    )
    payload = json.dumps(canonical_items, separators=(",", ":"), ensure_ascii=True)
    return sha256(payload.encode("utf-8")).hexdigest()


def _ensure_same_request(stored_fingerprint: str, request_fingerprint: str) -> None:
    if stored_fingerprint != request_fingerprint:
        raise IdempotencyConflict(
            "Idempotency-Key was already used with a different reservation request."
        )
