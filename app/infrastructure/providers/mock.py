from uuid import UUID

from app.application.ports.provider_gateway import (
    ProviderReleaseOutcome,
    ProviderReleaseResult,
    ProviderReservationLookupOutcome,
    ProviderReservationLookupResult,
    ProviderReserveOutcome,
    ProviderReserveResult,
)


class MockAvailabilityProvider:
    """Query-style provider.

    reserve() is implemented by an availability check. For this interview
    assignment, a positive query result is treated as an accepted external
    reservation decision.
    """

    def __init__(self, available_quantity: int = 100) -> None:
        self.available_quantity = available_quantity
        self.reserve_calls = 0
        self.release_calls = 0
        self.lookup_calls = 0
        self._accepted: set[str] = set()

    async def reserve(
        self,
        *,
        stock_source_id: UUID,
        quantity: int,
        reservation_key: str,
        expires_at,
    ) -> ProviderReserveResult:
        del stock_source_id, expires_at
        self.reserve_calls += 1
        if self.available_quantity < quantity:
            return ProviderReserveResult(
                outcome=ProviderReserveOutcome.DECLINED,
                error_code="MOCK_NOT_AVAILABLE",
            )

        self._accepted.add(reservation_key)
        return ProviderReserveResult(
            outcome=ProviderReserveOutcome.RESERVED,
            external_ref=f"query:{reservation_key}",
        )

    async def release(
        self,
        *,
        stock_source_id: UUID,
        external_ref: str,
        release_key: str,
    ) -> ProviderReleaseResult:
        del stock_source_id, external_ref, release_key
        self.release_calls += 1
        return ProviderReleaseResult(outcome=ProviderReleaseOutcome.RELEASED)

    async def get_reservation(
        self,
        *,
        reservation_key: str,
    ) -> ProviderReservationLookupResult:
        self.lookup_calls += 1
        if reservation_key in self._accepted:
            return ProviderReservationLookupResult(
                outcome=ProviderReservationLookupOutcome.RESERVED,
                external_ref=f"query:{reservation_key}",
            )
        return ProviderReservationLookupResult(
            outcome=ProviderReservationLookupOutcome.NOT_RESERVED
        )


class MockReservationProvider:
    """Hold-style provider with configurable deterministic outcomes."""

    def __init__(
        self,
        *,
        reserve_outcome: ProviderReserveOutcome = ProviderReserveOutcome.RESERVED,
        release_outcome: ProviderReleaseOutcome = ProviderReleaseOutcome.RELEASED,
        lookup_outcome: ProviderReservationLookupOutcome = ProviderReservationLookupOutcome.RESERVED,
    ) -> None:
        self.reserve_outcome = reserve_outcome
        self.release_outcome = release_outcome
        self.lookup_outcome = lookup_outcome
        self._refs: dict[str, str] = {}
        self.reserve_calls = 0
        self.release_calls = 0
        self.lookup_calls = 0

    async def reserve(
        self,
        *,
        stock_source_id: UUID,
        quantity: int,
        reservation_key: str,
        expires_at,
    ) -> ProviderReserveResult:
        del stock_source_id, quantity, expires_at
        self.reserve_calls += 1

        if self.reserve_outcome == ProviderReserveOutcome.DECLINED:
            return ProviderReserveResult(
                outcome=ProviderReserveOutcome.DECLINED,
                error_code="MOCK_RESERVATION_DECLINED",
            )
        if self.reserve_outcome == ProviderReserveOutcome.UNKNOWN:
            return ProviderReserveResult(
                outcome=ProviderReserveOutcome.UNKNOWN,
                error_code="MOCK_RESERVATION_UNKNOWN",
            )

        external_ref = self._refs.setdefault(
            reservation_key,
            f"mock-reservation:{reservation_key}",
        )
        return ProviderReserveResult(
            outcome=ProviderReserveOutcome.RESERVED,
            external_ref=external_ref,
        )

    async def release(
        self,
        *,
        stock_source_id: UUID,
        external_ref: str,
        release_key: str,
    ) -> ProviderReleaseResult:
        del stock_source_id, external_ref, release_key
        self.release_calls += 1
        if self.release_outcome == ProviderReleaseOutcome.UNKNOWN:
            return ProviderReleaseResult(
                outcome=ProviderReleaseOutcome.UNKNOWN,
                error_code="MOCK_RELEASE_UNKNOWN",
            )
        return ProviderReleaseResult(outcome=ProviderReleaseOutcome.RELEASED)

    async def get_reservation(
        self,
        *,
        reservation_key: str,
    ) -> ProviderReservationLookupResult:
        self.lookup_calls += 1
        if self.lookup_outcome == ProviderReservationLookupOutcome.UNKNOWN:
            return ProviderReservationLookupResult(
                outcome=ProviderReservationLookupOutcome.UNKNOWN,
                error_code="MOCK_LOOKUP_UNKNOWN",
            )
        if self.lookup_outcome == ProviderReservationLookupOutcome.NOT_RESERVED:
            return ProviderReservationLookupResult(
                outcome=ProviderReservationLookupOutcome.NOT_RESERVED
            )

        external_ref = self._refs.setdefault(
            reservation_key,
            f"mock-reservation:{reservation_key}",
        )
        return ProviderReservationLookupResult(
            outcome=ProviderReservationLookupOutcome.RESERVED,
            external_ref=external_ref,
        )
