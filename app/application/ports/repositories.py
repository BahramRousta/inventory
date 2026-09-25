from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.application.dto.reservations import (
    ReservationIdentityRecord,
    ReservationItemCommand,
    ReservationLineResult,
    StockSourceRecord,
)
from app.domain.enums import ReservationStatus


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

    async def add_held_line(self, reservation_id: UUID, item: ReservationItemCommand) -> None: ...

    async def set_status(self, reservation_id: UUID, status: ReservationStatus) -> None: ...

    async def get_lines(self, reservation_id: UUID) -> tuple[ReservationLineResult, ...]: ...


class InternalInventoryRepository(Protocol):
    async def try_hold(self, stock_source_id: UUID, quantity: int) -> bool: ...


class UnitOfWork(Protocol):
    stock_sources: StockSourceRepository
    reservations: ReservationRepository
    inventory: InternalInventoryRepository

    async def __aenter__(self) -> "UnitOfWork": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
