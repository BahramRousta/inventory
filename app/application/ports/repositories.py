from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.application.dto.reservations import (
    ReservationIdentityRecord,
    ExternalReleaseRecord,
    PendingExternalHoldRecord,
    ClaimedExternalHoldRecord,
    ClaimedExternalReleaseRecord,
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

    async def get_by_id(self, reservation_id: UUID) -> ReservationIdentityRecord | None: ...

    async def create(
        self,
        *,
        reservation_id: UUID,
        user_id: str,
        idempotency_key: str,
        request_fingerprint: str,
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
        claim_token: UUID,
        status: ReservationLineStatus,
        external_hold_ref: str | None,
    ) -> bool: ...

    async def activate_if_all_lines_held(self, reservation_id: UUID) -> bool: ...
    async def begin_confirming_if_active(self, reservation_id: UUID) -> bool: ...

    async def fail_pending_hold_for_release(
        self, reservation_id: UUID, stock_source_id: UUID
    ) -> bool: ...

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
        claim_token: UUID,
        status: ReservationLineStatus,
    ) -> bool: ...

    async def cancel_if_all_lines_resolved(self, reservation_id: UUID) -> bool: ...

    async def get_next_pending_external_hold(
        self,
    ) -> PendingExternalHoldRecord | None: ...

    async def is_external_hold_pending(
        self, reservation_id: UUID, stock_source_id: UUID
    ) -> bool: ...

    async def is_external_hold_claim_owned(
        self, reservation_id: UUID, stock_source_id: UUID, claim_token: UUID
    ) -> bool: ...

    async def begin_releasing_if_reserving(self, reservation_id: UUID) -> bool: ...

    async def is_external_release_pending(
        self, reservation_id: UUID, stock_source_id: UUID
    ) -> bool: ...

    async def is_external_release_claim_owned(
        self, reservation_id: UUID, stock_source_id: UUID, claim_token: UUID
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

    async def claim_pending_external_holds(
        self, *, limit: int, lease_seconds: int
    ) -> tuple[ClaimedExternalHoldRecord, ...]: ...

    async def claim_pending_external_releases(
        self, *, limit: int, lease_seconds: int
    ) -> tuple[ClaimedExternalReleaseRecord, ...]: ...

    async def claim_unknown_external_holds(
        self, *, limit: int, lease_seconds: int
    ) -> tuple[ClaimedExternalHoldRecord, ...]: ...

    async def claim_unknown_external_releases(
        self, *, limit: int, lease_seconds: int
    ) -> tuple[ClaimedExternalReleaseRecord, ...]: ...

    async def recover_expired_provider_claims(self, *, limit: int) -> int: ...

    async def claim_expired_reserving_reservations(
        self, *, limit: int
    ) -> tuple[UUID, ...]: ...

    async def return_hold_claim_to_unknown(
        self, reservation_id: UUID, stock_source_id: UUID, claim_token: UUID
    ) -> bool: ...

    async def return_release_claim_to_unknown(
        self, reservation_id: UUID, stock_source_id: UUID, claim_token: UUID
    ) -> bool: ...

    async def return_release_claim_to_pending(
        self, reservation_id: UUID, stock_source_id: UUID, claim_token: UUID
    ) -> bool: ...

    async def set_status(self, reservation_id: UUID, status: ReservationStatus) -> None: ...

    async def get_lines(self, reservation_id: UUID) -> tuple[ReservationLineResult, ...]: ...
    async def get_external_hold_ref(
        self, reservation_id: UUID, stock_source_id: UUID
    ) -> str | None: ...
    async def mark_line_confirmed(self, reservation_id: UUID, stock_source_id: UUID) -> None: ...

    async def begin_releasing(self, reservation_id: UUID, reason: str) -> bool: ...

    async def confirm_if_all_lines_confirmed(self, reservation_id: UUID) -> bool: ...

class InternalInventoryRepository(Protocol):
    async def try_hold(self, stock_source_id: UUID, quantity: int) -> bool: ...

    async def release_hold(self, stock_source_id: UUID, quantity: int) -> bool: ...

    async def consume_hold(self, stock_source_id: UUID, quantity: int) -> bool: ...


class OrderRepository(Protocol):
    async def get_by_reservation_id(self, reservation_id: UUID) -> UUID | None: ...

    async def create(self, *, reservation_id: UUID, user_id: str) -> UUID: ...


class UnitOfWork(Protocol):
    stock_sources: StockSourceRepository
    reservations: ReservationRepository
    inventory: InternalInventoryRepository
    orders: OrderRepository

    async def __aenter__(self) -> "UnitOfWork": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
