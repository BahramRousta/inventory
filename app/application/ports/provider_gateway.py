from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from collections.abc import Mapping
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
class ProviderHoldResult:
    """Provider-neutral outcome for one external inventory HOLD attempt.

    `UNKNOWN` means the provider may have created the hold but the caller could
    not establish its outcome. It must later be reconciled with the same key.
    """

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
    """Application-owned boundary for provider-specific HOLD implementations."""

    async def hold(
        self,
        *,
        stock_source_id: UUID,
        quantity: int,
        hold_key: str,
        expires_at: datetime,
    ) -> ProviderHoldResult:
        """Attempt an exclusive upstream hold without exposing HTTP details."""
        ...

    async def release(
        self,
        *,
        stock_source_id: UUID,
        external_hold_ref: str,
        release_key: str,
    ) -> ProviderReleaseResult:
        """Release an upstream hold using a stable idempotency key."""
        ...

    async def get_hold(self, *, hold_key: str) -> ProviderHoldLookupResult:
        """Query the upstream state of the original logical HOLD."""
        ...


class ProviderGatewayRegistry(Protocol):
    """Resolves the configured gateway for an external inventory provider."""

    def get(self, provider_id: UUID) -> ProviderGateway | None: ...


class InMemoryProviderGatewayRegistry:
    """Small bootstrap registry; provider adapters are registered explicitly."""

    def __init__(self, gateways: Mapping[UUID, ProviderGateway] | None = None) -> None:
        self._gateways = dict(gateways or {})

    def get(self, provider_id: UUID) -> ProviderGateway | None:
        return self._gateways.get(provider_id)
