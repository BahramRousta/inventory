from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class ProviderReserveOutcome(StrEnum):
    RESERVED = "RESERVED"
    DECLINED = "DECLINED"
    UNKNOWN = "UNKNOWN"


class ProviderReleaseOutcome(StrEnum):
    RELEASED = "RELEASED"
    UNKNOWN = "UNKNOWN"


class ProviderReservationLookupOutcome(StrEnum):
    RESERVED = "RESERVED"
    NOT_RESERVED = "NOT_RESERVED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProviderReserveResult:
    outcome: ProviderReserveOutcome
    external_ref: str | None = None
    external_expires_at: datetime | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class ProviderReleaseResult:
    outcome: ProviderReleaseOutcome
    error_code: str | None = None


@dataclass(frozen=True)
class ProviderReservationLookupResult:
    outcome: ProviderReservationLookupOutcome
    external_ref: str | None = None
    error_code: str | None = None


class InventoryProvider(Protocol):
    """One application-facing provider contract.

    Each provider hides how reservation is achieved. A query provider can
    implement reserve() by checking availability; a hold-capable provider can
    implement reserve() by calling its HOLD operation.
    """

    async def reserve(
        self,
        *,
        stock_source_id: UUID,
        quantity: int,
        reservation_key: str,
        expires_at: datetime,
    ) -> ProviderReserveResult: ...

    async def release(
        self,
        *,
        stock_source_id: UUID,
        external_ref: str,
        release_key: str,
    ) -> ProviderReleaseResult: ...

    async def get_reservation(
        self,
        *,
        reservation_key: str,
    ) -> ProviderReservationLookupResult: ...


class ProviderRegistryProtocol(Protocol):
    def get(self, provider_id: UUID) -> InventoryProvider | None: ...


class ProviderRegistry:
    def __init__(
        self,
        providers: Mapping[UUID, InventoryProvider] | None = None,
    ) -> None:
        self._providers = dict(providers or {})

    def get(self, provider_id: UUID) -> InventoryProvider | None:
        return self._providers.get(provider_id)
