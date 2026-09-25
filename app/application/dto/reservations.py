from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.enums import (
    PaymentOutcome,
    ProviderKind,
    ReservationLineStatus,
    ReservationStatus,
)


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
class PaymentOutcomeCommand:
    event_id: UUID
    reservation_id: UUID
    user_id: str
    outcome: PaymentOutcome


@dataclass(frozen=True)
class StockSourceRecord:
    stock_source_id: UUID
    product_id: UUID
    provider_id: UUID
    provider_kind: ProviderKind
    provider_enabled: bool
    source_enabled: bool

@dataclass(frozen=True)
class ReservationIdentityRecord:
    reservation_id: UUID
    user_id: str
    idempotency_key: str
    request_fingerprint: str | None
    status: ReservationStatus
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class ReservationLineResult:
    product_id: UUID
    stock_source_id: UUID
    quantity: int
    status: ReservationLineStatus


@dataclass(frozen=True)
class ExternalReleaseRecord:
    stock_source_id: UUID
    external_hold_ref: str


@dataclass(frozen=True)
class PendingExternalHoldRecord:
    reservation_id: UUID
    stock_source_id: UUID
    provider_id: UUID
    quantity: int
    expires_at: datetime


@dataclass(frozen=True)
class ClaimedExternalHoldRecord:
    reservation_id: UUID
    stock_source_id: UUID
    provider_id: UUID
    quantity: int
    expires_at: datetime
    claim_token: UUID


@dataclass(frozen=True)
class ClaimedExternalReleaseRecord:
    reservation_id: UUID
    stock_source_id: UUID
    provider_id: UUID
    external_hold_ref: str
    claim_token: UUID


@dataclass(frozen=True)
class PendingExternalReleaseRecord:
    reservation_id: UUID
    stock_source_id: UUID
    provider_id: UUID
    external_hold_ref: str


@dataclass(frozen=True)
class CreateReservationResult:
    reservation_id: UUID
    status: ReservationStatus
    created_at: datetime
    expires_at: datetime
    payment_allowed: bool
    requires_attention: bool
    lines: tuple[ReservationLineResult, ...]
    replayed: bool = False


@dataclass(frozen=True)
class ConfirmReservationResult:
    reservation_id: UUID
    order_id: UUID
    status: ReservationStatus
    created_at: datetime
    expires_at: datetime
    payment_allowed: bool
    requires_attention: bool
    lines: tuple[ReservationLineResult, ...]


@dataclass(frozen=True)
class PaymentOutcomeResult:
    reservation_id: UUID
    order_id: UUID | None
    status: ReservationStatus
    created_at: datetime
    expires_at: datetime
    payment_allowed: bool
    requires_attention: bool
    lines: tuple[ReservationLineResult, ...]
