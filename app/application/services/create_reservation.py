from datetime import timedelta
from typing import Callable
from uuid import UUID, uuid4

from app.application.dto.reservations import (
    CreateReservationCommand,
    CreateReservationResult,
    ReservationItemCommand,
    ReservationLineResult,
)
from app.application.errors import (
    IdempotencyConflict,
    InsufficientStock,
    PersistenceConflict,
    ProductSourceMismatch,
    SourceDisabled,
    SourceNotReservable,
)
from app.application.ports.clock import Clock
from app.application.ports.provider_gateway import (
    InMemoryProviderGatewayRegistry,
    ProviderGatewayRegistry,
)
from app.application.ports.repositories import UnitOfWork
from app.domain.enums import ProviderKind, ReservationLineStatus, ReservationStatus


class CreateReservationService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        ttl_seconds: int,
        provider_gateways: ProviderGatewayRegistry | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._provider_gateways = provider_gateways or InMemoryProviderGatewayRegistry()

    async def execute(self, command: CreateReservationCommand) -> CreateReservationResult:
        try:
            async with self._uow_factory() as uow:
                existing = await uow.reservations.get_by_idempotency_key(
                    command.user_id, command.idempotency_key
                )
                if existing is not None:
                    return self._return_or_reject_idempotent_replay(
                        existing.reservation_id,
                        existing.status,
                        existing.expires_at,
                        await uow.reservations.get_lines(existing.reservation_id),
                        command.items,
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

                has_external_lines = False
                for item in command.items:
                    source = sources[item.stock_source_id]
                    if source.provider_kind == ProviderKind.INTERNAL:
                        if not await uow.inventory.try_hold(
                            item.stock_source_id, item.quantity
                        ):
                            raise InsufficientStock(
                                f"Insufficient stock for source {item.stock_source_id}."
                            )
                        await uow.reservations.add_line(
                            reservation_id, item, ReservationLineStatus.HELD
                        )
                        continue

                    has_external_lines = True
                    await uow.reservations.add_line(
                        reservation_id, item, ReservationLineStatus.HOLD_PENDING
                    )

                reservation_status = (
                    ReservationStatus.RESERVING
                    if has_external_lines
                    else ReservationStatus.ACTIVE
                )
                if reservation_status == ReservationStatus.ACTIVE:
                    await uow.reservations.set_status(
                        reservation_id, ReservationStatus.ACTIVE
                    )
                lines = await uow.reservations.get_lines(reservation_id)
                await uow.commit()
                return CreateReservationResult(
                    reservation_id=reservation_id,
                    status=reservation_status,
                    expires_at=expires_at,
                    payment_allowed=reservation_status == ReservationStatus.ACTIVE,
                    lines=lines,
                )
        except PersistenceConflict:
            async with self._uow_factory() as retry_uow:
                existing = await retry_uow.reservations.get_by_idempotency_key(
                    command.user_id, command.idempotency_key
                )
                if existing is None:
                    raise
                return self._return_or_reject_idempotent_replay(
                    existing.reservation_id,
                    existing.status,
                    existing.expires_at,
                    await retry_uow.reservations.get_lines(existing.reservation_id),
                    command.items,
                )

    def _return_or_reject_idempotent_replay(
        self,
        reservation_id: UUID,
        status: ReservationStatus,
        expires_at,
        lines: tuple[ReservationLineResult, ...],
        requested_items: tuple[ReservationItemCommand, ...],
    ) -> CreateReservationResult:
        persisted = sorted(
            (
                line.product_id,
                line.stock_source_id,
                line.quantity,
            )
            for line in lines
        )
        requested = sorted(
            (
                item.product_id,
                item.stock_source_id,
                item.quantity,
            )
            for item in requested_items
        )
        if persisted != requested:
            raise IdempotencyConflict(
                "The idempotency key was already used with a different request payload."
            )
        return CreateReservationResult(
            reservation_id=reservation_id,
            status=status,
            expires_at=expires_at,
            payment_allowed=status == ReservationStatus.ACTIVE,
            lines=lines,
        )

    def _validate_sources(self, items, sources) -> None:
        for item in items:
            source = sources.get(item.stock_source_id)
            if source is None or source.product_id != item.product_id:
                raise ProductSourceMismatch(
                    f"Source {item.stock_source_id} does not belong to product {item.product_id}."
                )
            if not source.source_enabled or not source.provider_enabled:
                raise SourceDisabled(f"Source {item.stock_source_id} is disabled.")
            if not source.reservation_supported:
                raise SourceNotReservable(
                    f"Source {item.stock_source_id} is not reservable."
                )
            if (
                source.provider_kind == ProviderKind.EXTERNAL
                and self._provider_gateways.get(source.provider_id) is None
            ):
                raise SourceNotReservable(
                    f"Provider {source.provider_id} has no registered hold gateway."
                )
