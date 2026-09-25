import hashlib
import json
from collections import defaultdict
from datetime import timedelta
from typing import Callable
from uuid import UUID, uuid4

from sqlalchemy.util import await_

from app.application.dto.reservations import (
    CreateReservationCommand,
    CreateReservationResult,
    ReservationItemCommand, ReservationLineResult,
)
from app.application.errors import (
    IdempotencyConflict,
    InsufficientStock,
    InvalidReservationItems,
    PersistenceConflict,
    ProductSourceMismatch,
    SourceDisabled,
    SourceNotReservable,
)
from app.application.ports.clock import Clock
from app.application.ports.repositories import UnitOfWork
from app.domain.enums import ProviderKind, ReservationStatus


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
        """
        1- check idempotency and avoid duplication
        2- validate product belong to the sources
        3- create reservation row
        4- per item in reservation try to hold the stock, if any item fails, raise InsufficientStock
        5- set reservation status to ACTIVE
        6- return reservation result
        :param command:
        :return:
        """
        try:
            async with self._uow_factory() as uow:
                existing = await uow.reservations.get_by_idempotency_key(
                    command.user_id, command.idempotency_key
                )
                if existing is not None:
                    return await self._return_or_reject_idempotent_replay(
                        existing.reservation_id,
                        existing.status, existing.expires_at,
                        await uow.reservations.get_lines(existing.reservation_id)
                    )

                sources = await uow.stock_sources.get_many(
                    tuple(item.stock_source_id for item in command.items)
                )
                self._validate_sources(command.items, sources)

                reservation_id = uuid4()
                expires_at = self._clock.now() + timedelta(seconds=self._ttl_seconds)

                await uow.reservations.create(
                    reservation_id=reservation_id,
                    user_id=command.user_id,
                    idempotency_key=command.idempotency_key,
                    expires_at=expires_at,
                    status=ReservationStatus.RESERVING,
                )

                for item in command.items:
                    if not await uow.inventory.try_hold(item.stock_source_id, item.quantity):
                        raise InsufficientStock(
                            f"Insufficient stock for source {item.stock_source_id}."
                        )
                    await uow.reservations.add_held_line(reservation_id, item)

                await uow.reservations.set_status(reservation_id, ReservationStatus.ACTIVE)
                lines = await uow.reservations.get_lines(reservation_id)
                await uow.commit()
                return CreateReservationResult(
                    reservation_id=reservation_id,
                    status=ReservationStatus.ACTIVE,
                    expires_at=expires_at,
                    payment_allowed=True,
                    lines=lines,
                )
        except PersistenceConflict:
            async with self._uow_factory() as retry_uow:
                existing = await retry_uow.reservations.get_by_idempotency_key(
                    command.user_id, command.idempotency_key
                )
                if existing is None:
                    raise
                return await self._return_or_reject_idempotent_replay(
                    existing.reservation_id,
                    existing.status,
                    existing.expires_at,
                    lines=await retry_uow.reservations.get_lines(existing.reservation_id)
                )

    async def _return_or_reject_idempotent_replay(
        self,
        reservation_id: UUID,
        status: ReservationStatus,
        expires_at,
            lines: tuple[ReservationLineResult, ...]
    ) -> CreateReservationResult:
        return CreateReservationResult(
            reservation_id=reservation_id,
            status=status,
            expires_at=expires_at,
            payment_allowed=status == ReservationStatus.ACTIVE,
            lines=lines,
        )

    @staticmethod
    def _validate_sources(items, sources) -> None:
        for item in items:
            source = sources.get(item.stock_source_id)
            if source is None or source.product_id != item.product_id:
                raise ProductSourceMismatch(
                    f"Source {item.stock_source_id} does not belong to product {item.product_id}."
                )
            if not source.source_enabled or not source.provider_enabled:
                raise SourceDisabled(f"Source {item.stock_source_id} is disabled.")
            if source.provider_kind != ProviderKind.INTERNAL or not source.reservation_supported:
                raise SourceNotReservable(
                    f"Source {item.stock_source_id} is not supported by the internal-only slice."
                )
