import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid5

from app.application.ports.provider_gateway import (
    ProviderGateway,
    ProviderHoldLookupOutcome,
    ProviderHoldLookupResult,
    ProviderHoldOutcome,
    ProviderHoldResult,
    ProviderReleaseOutcome,
    ProviderReleaseResult,
)


@dataclass
class _Hold:
    reference: str
    released: bool = False


class InMemoryExclusiveHoldGateway(ProviderGateway):
    """Process-local provider simulator for development and seeded demo data."""

    def __init__(self, provider_id: UUID) -> None:
        self._provider_id = provider_id
        self._holds_by_key: dict[str, _Hold] = {}
        self._holds_by_reference: dict[str, _Hold] = {}
        self._lock = asyncio.Lock()

    async def hold(
        self,
        *,
        stock_source_id: UUID,
        quantity: int,
        hold_key: str,
        expires_at: datetime,
    ) -> ProviderHoldResult:
        del stock_source_id, quantity
        async with self._lock:
            hold = self._holds_by_key.get(hold_key)
            if hold is None:
                reference = str(uuid5(self._provider_id, hold_key))
                hold = _Hold(reference=reference)
                self._holds_by_key[hold_key] = hold
                self._holds_by_reference[reference] = hold
            if hold.released:
                return ProviderHoldResult(
                    outcome=ProviderHoldOutcome.UNKNOWN,
                    error_code="IN_MEMORY_HOLD_ALREADY_RELEASED",
                )
            return ProviderHoldResult(
                outcome=ProviderHoldOutcome.HELD,
                external_hold_ref=hold.reference,
                external_expires_at=expires_at + timedelta(minutes=2),
            )

    async def release(
        self,
        *,
        stock_source_id: UUID,
        external_hold_ref: str,
        release_key: str,
    ) -> ProviderReleaseResult:
        del stock_source_id, release_key
        async with self._lock:
            hold = self._holds_by_reference.get(external_hold_ref)
            if hold is None:
                return ProviderReleaseResult(ProviderReleaseOutcome.UNKNOWN)
            hold.released = True
            return ProviderReleaseResult(ProviderReleaseOutcome.RELEASED)

    async def get_hold(self, *, hold_key: str) -> ProviderHoldLookupResult:
        async with self._lock:
            hold = self._holds_by_key.get(hold_key)
            if hold is None or hold.released:
                return ProviderHoldLookupResult(ProviderHoldLookupOutcome.NOT_HELD)
            return ProviderHoldLookupResult(
                outcome=ProviderHoldLookupOutcome.HELD,
                external_hold_ref=hold.reference,
            )
