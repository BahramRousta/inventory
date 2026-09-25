from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.application.dto.reservations import (
    ReservationIdentityRecord,
    ExternalReleaseRecord,
    PendingExternalHoldRecord,
    PendingExternalReleaseRecord,
    ReservationItemCommand,
    ReservationLineResult,
    StockSourceRecord,
)
from app.domain.enums import ReservationLineStatus, ReservationStatus


class StockSourceRepository(Protocol):
    async def get_many(self, source_ids: tuple[UUID, ...]) -> dict[UUID, StockSourceRecord]: ...


class ReservationRepository(Protocol):
    async def get_by_idempotency_key(
        self, user_id: str, idempotency_key: str
    ) -> ReservationIdentityRecord | None: ...

    async def create(
        self,
        *,
        reservation_id: UUID,
        user_id: str,
        idempotency_key: str,
        expires_at: datetime,
        status: ReservationStatus,
    ) -> None: ...

    async def add_line(
        self,
        reservation_id: UUID,
        item: ReservationItemCommand,
        status: ReservationLineStatus,
    ) -> None: ...

    async def record_external_hold_result(
        self,
        *,
        reservation_id: UUID,
        stock_source_id: UUID,
        status: ReservationLineStatus,
        external_hold_ref: str | None,
    ) -> None: ...

    async def activate_if_all_lines_held(self, reservation_id: UUID) -> bool: ...

    async def claim_line_for_release(
        self, reservation_id: UUID, stock_source_id: UUID
    ) -> bool: ...

    async def mark_line_released(
        self, reservation_id: UUID, stock_source_id: UUID
    ) -> None: ...

    async def get_pending_external_releases(
        self, reservation_id: UUID
    ) -> tuple[ExternalReleaseRecord, ...]: ...

    async def record_external_release_result(
        self,
        *,
        reservation_id: UUID,
        stock_source_id: UUID,
        status: ReservationLineStatus,
    ) -> None: ...

    async def cancel_if_all_lines_resolved(self, reservation_id: UUID) -> bool: ...

    async def get_next_pending_external_hold(
        self,
    ) -> PendingExternalHoldRecord | None: ...

    async def is_external_hold_pending(
        self, reservation_id: UUID, stock_source_id: UUID
    ) -> bool: ...

    async def begin_releasing_if_reserving(self, reservation_id: UUID) -> bool: ...

    async def is_external_release_pending(
        self, reservation_id: UUID, stock_source_id: UUID
    ) -> bool: ...

    async def is_releasing(self, reservation_id: UUID) -> bool: ...

    async def get_next_unknown_external_hold(
        self,
    ) -> PendingExternalHoldRecord | None: ...

    async def get_next_unknown_external_release(
        self,
    ) -> PendingExternalReleaseRecord | None: ...

    async def reconcile_external_hold_result(
        self,
        *,
        reservation_id: UUID,
        stock_source_id: UUID,
        status: ReservationLineStatus,
        external_hold_ref: str | None,
    ) -> None: ...

    async def mark_unknown_release_released(
        self, reservation_id: UUID, stock_source_id: UUID
    ) -> None: ...

    async def restore_release_pending_from_unknown(
        self, reservation_id: UUID, stock_source_id: UUID
    ) -> None: ...

    async def claim_next_expired_reserving_reservation(self) -> UUID | None: ...

    async def get_next_releasing_reservation_id(self) -> UUID | None: ...

    async def set_status(self, reservation_id: UUID, status: ReservationStatus) -> None: ...

    async def get_lines(self, reservation_id: UUID) -> tuple[ReservationLineResult, ...]: ...


class InternalInventoryRepository(Protocol):
    async def try_hold(self, stock_source_id: UUID, quantity: int) -> bool: ...

    async def release_hold(self, stock_source_id: UUID, quantity: int) -> bool: ...


class UnitOfWork(Protocol):
    stock_sources: StockSourceRepository
    reservations: ReservationRepository
    inventory: InternalInventoryRepository

    async def __aenter__(self) -> "UnitOfWork": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
