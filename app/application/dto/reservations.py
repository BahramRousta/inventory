import uuid
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.enums import ProviderKind, ReservationLineStatus, ReservationStatus


@dataclass(frozen=True)
class ReservationItemCommand:
    product_id: UUID
    stock_source_id: UUID
    quantity: int


@dataclass(frozen=True)
class CreateReservationCommand:
    user_id: str
    idempotency_key: str
    items: tuple[ReservationItemCommand, ...]


@dataclass(frozen=True)
class StockSourceRecord:
    stock_source_id: UUID
    product_id: UUID
    provider_id: UUID
    provider_kind: ProviderKind
    provider_enabled: bool
    source_enabled: bool
    reservation_supported: bool


@dataclass(frozen=True)
class ReservationIdentityRecord:
    reservation_id: UUID
    user_id: str
    idempotency_key: str
    status: ReservationStatus
    expires_at: datetime


@dataclass(frozen=True)
class ReservationLineResult:
    product_id: UUID
    stock_source_id: UUID
    quantity: int
    status: ReservationLineStatus


@dataclass(frozen=True)
class CreateReservationResult:
    reservation_id: UUID
    status: ReservationStatus
    expires_at: datetime
    payment_allowed: bool
    lines: tuple[ReservationLineResult, ...]
