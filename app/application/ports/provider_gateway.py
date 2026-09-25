from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_check: bool = False
    supports_hold: bool = False
    supports_release: bool = False
    supports_get_hold: bool = False
    hold_is_final_allocation: bool = False

    @property
    def supports_reservation_workflow(self) -> bool:
        return (
            self.supports_hold
            and self.supports_release
            and self.supports_get_hold
            and self.hold_is_final_allocation
        )


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
class ProviderHoldResult:
    """Provider-neutral outcome for one external inventory HOLD attempt."""

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


class ProviderGateway(Protocol):
    """Application-owned boundary for one provider adapter."""

    capabilities: ProviderCapabilities

    async def hold(
        self,
        *,
        stock_source_id: UUID,
        quantity: int,
        hold_key: str,
        expires_at: datetime,
    ) -> ProviderHoldResult: ...

    async def release(
        self,
        *,
        stock_source_id: UUID,
        external_hold_ref: str,
        release_key: str,
    ) -> ProviderReleaseResult: ...

    async def get_hold(self, *, hold_key: str) -> ProviderHoldLookupResult: ...


class ProviderGatewayRegistry(Protocol):
    def get(self, provider_id: UUID) -> ProviderGateway | None: ...


class InMemoryProviderGatewayRegistry:
    """Runtime registry populated by the infrastructure provider factory."""

    def __init__(self, gateways: Mapping[UUID, ProviderGateway] | None = None) -> None:
        self._gateways = dict(gateways or {})

    def get(self, provider_id: UUID) -> ProviderGateway | None:
        return self._gateways.get(provider_id)
