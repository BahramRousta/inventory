from uuid import UUID

from app.application.ports.provider_gateway import (
    ProviderAvailabilityResult,
    ProviderHoldLookupOutcome,
    ProviderHoldLookupResult,
    ProviderHoldOutcome,
    ProviderHoldResult,
    ProviderReleaseOutcome,
    ProviderReleaseResult,
)


class MockAvailabilityProviderGateway:
    """Assignment-only query provider with a deterministic response."""

    def __init__(self, available_quantity: int = 100) -> None:
        self.available_quantity = available_quantity

    async def check_availability(
        self,
        *,
        stock_source_id: UUID,
    ) -> ProviderAvailabilityResult:
        del stock_source_id
        return ProviderAvailabilityResult(
            available_quantity=self.available_quantity
        )


class MockReservationProviderGateway:
    """Assignment-only reservation provider.

    It deliberately avoids real HTTP/provider integration. Outcomes are
    configurable so services and workers can demonstrate happy, declined, and
    ambiguous provider behavior.
    """

    hold_is_final_allocation = True

    def __init__(
        self,
        *,
        hold_outcome: ProviderHoldOutcome = ProviderHoldOutcome.HELD,
        release_outcome: ProviderReleaseOutcome = ProviderReleaseOutcome.RELEASED,
        lookup_outcome: ProviderHoldLookupOutcome = ProviderHoldLookupOutcome.HELD,
    ) -> None:
        self.hold_outcome = hold_outcome
        self.release_outcome = release_outcome
        self.lookup_outcome = lookup_outcome
        self._hold_refs: dict[str, str] = {}

    async def hold(
        self,
        *,
        stock_source_id: UUID,
        quantity: int,
        hold_key: str,
        expires_at,
    ) -> ProviderHoldResult:
        del stock_source_id, quantity, expires_at
        if self.hold_outcome == ProviderHoldOutcome.DECLINED:
            return ProviderHoldResult(
                outcome=ProviderHoldOutcome.DECLINED,
                error_code="MOCK_HOLD_DECLINED",
            )
        if self.hold_outcome == ProviderHoldOutcome.UNKNOWN:
            return ProviderHoldResult(
                outcome=ProviderHoldOutcome.UNKNOWN,
                error_code="MOCK_HOLD_UNKNOWN",
            )

        hold_ref = self._hold_refs.setdefault(hold_key, f"mock-hold:{hold_key}")
        return ProviderHoldResult(
            outcome=ProviderHoldOutcome.HELD,
            external_hold_ref=hold_ref,
        )

    async def release(
        self,
        *,
        stock_source_id: UUID,
        external_hold_ref: str,
        release_key: str,
    ) -> ProviderReleaseResult:
        del stock_source_id, external_hold_ref, release_key
        if self.release_outcome == ProviderReleaseOutcome.UNKNOWN:
            return ProviderReleaseResult(
                outcome=ProviderReleaseOutcome.UNKNOWN,
                error_code="MOCK_RELEASE_UNKNOWN",
            )
        return ProviderReleaseResult(outcome=ProviderReleaseOutcome.RELEASED)

    async def get_hold(self, *, hold_key: str) -> ProviderHoldLookupResult:
        if self.lookup_outcome == ProviderHoldLookupOutcome.UNKNOWN:
            return ProviderHoldLookupResult(
                outcome=ProviderHoldLookupOutcome.UNKNOWN,
                error_code="MOCK_LOOKUP_UNKNOWN",
            )
        if self.lookup_outcome == ProviderHoldLookupOutcome.NOT_HELD:
            return ProviderHoldLookupResult(
                outcome=ProviderHoldLookupOutcome.NOT_HELD
            )

        hold_ref = self._hold_refs.setdefault(hold_key, f"mock-hold:{hold_key}")
        return ProviderHoldLookupResult(
            outcome=ProviderHoldLookupOutcome.HELD,
            external_hold_ref=hold_ref,
        )
