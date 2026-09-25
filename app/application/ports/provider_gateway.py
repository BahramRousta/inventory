from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class ProviderHoldOutcome(StrEnum):
    HELD = "HELD"
    DECLINED = "DECLINED"
    UNKNOWN = "UNKNOWN"


class ProviderReleaseOutcome(StrEnum):
    RELEASED = "RELEASED"
    UNKNOWN = "UNKNOWN"


class ProviderHoldLookupOutcome(StrEnum):
    HELD = "HELD"
    NOT_HELD = "NOT_HELD"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProviderAvailabilityResult:
    available_quantity: int


@dataclass(frozen=True)
class ProviderHoldResult:
    outcome: ProviderHoldOutcome
    external_hold_ref: str | None = None
    external_expires_at: datetime | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class ProviderReleaseResult:
    outcome: ProviderReleaseOutcome
    error_code: str | None = None


@dataclass(frozen=True)
class ProviderHoldLookupResult:
    outcome: ProviderHoldLookupOutcome
    external_hold_ref: str | None = None
    error_code: str | None = None


class AvailabilityProviderGateway(Protocol):
    """Provider that can only answer availability queries."""

    async def check_availability(
        self,
        *,
        stock_source_id: UUID,
    ) -> ProviderAvailabilityResult: ...


class HoldProviderGateway(Protocol):
    async def hold(
        self,
        *,
        stock_source_id: UUID,
        quantity: int,
        hold_key: str,
        expires_at: datetime,
    ) -> ProviderHoldResult: ...


class ReleaseProviderGateway(Protocol):
    async def release(
        self,
        *,
        stock_source_id: UUID,
        external_hold_ref: str,
        release_key: str,
    ) -> ProviderReleaseResult: ...


class HoldStatusProviderGateway(Protocol):
    async def get_hold(self, *, hold_key: str) -> ProviderHoldLookupResult: ...


class ReservationProviderGateway(
    HoldProviderGateway,
    ReleaseProviderGateway,
    HoldStatusProviderGateway,
    Protocol,
):
    """Provider contract required by the checkout reservation workflow."""

    hold_is_final_allocation: bool


class ProviderGatewayRegistry(Protocol):
    def get_reservation_provider(
        self, provider_id: UUID
    ) -> ReservationProviderGateway | None: ...

    def get_availability_provider(
        self, provider_id: UUID
    ) -> AvailabilityProviderGateway | None: ...


class ProviderRegistry:
    """Small runtime registry populated by the provider factory."""

    def __init__(
        self,
        *,
        reservation_providers: Mapping[UUID, ReservationProviderGateway] | None = None,
        availability_providers: Mapping[UUID, AvailabilityProviderGateway] | None = None,
    ) -> None:
        self._reservation_providers = dict(reservation_providers or {})
        self._availability_providers = dict(availability_providers or {})

    def get_reservation_provider(
        self, provider_id: UUID
    ) -> ReservationProviderGateway | None:
        return self._reservation_providers.get(provider_id)

    def get_availability_provider(
        self, provider_id: UUID
    ) -> AvailabilityProviderGateway | None:
        return self._availability_providers.get(provider_id)
