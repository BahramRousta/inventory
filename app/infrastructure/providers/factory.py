from app.application.ports.provider_gateway import (
    ProviderRegistry,
    ProviderRegistryProtocol,
    ProviderReleaseOutcome,
    ProviderReservationLookupOutcome,
    ProviderReserveOutcome,
)
from app.bootstrap.config import Settings
from app.infrastructure.providers.mock import (
    MockAvailabilityProvider,
    MockReservationProvider,
)


class ProviderFactory:
    """Build provider implementations behind one application-facing interface."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def create_registry(self) -> ProviderRegistryProtocol:
        providers = {}

        if self._settings.external_provider_id is not None:
            reserve_outcome, release_outcome, lookup_outcome = _mode_outcomes(
                self._settings.mock_provider_mode
            )
            providers[self._settings.external_provider_id] = (
                MockReservationProvider(
                    reserve_outcome=reserve_outcome,
                    release_outcome=release_outcome,
                    lookup_outcome=lookup_outcome,
                )
            )

        if self._settings.query_only_provider_id is not None:
            providers[self._settings.query_only_provider_id] = (
                MockAvailabilityProvider(
                    available_quantity=(
                        self._settings.mock_provider_available_quantity
                    )
                )
            )

        return ProviderRegistry(providers)


def _mode_outcomes(mode: str):
    if mode == "success":
        return (
            ProviderReserveOutcome.RESERVED,
            ProviderReleaseOutcome.RELEASED,
            ProviderReservationLookupOutcome.RESERVED,
        )
    if mode == "decline":
        return (
            ProviderReserveOutcome.DECLINED,
            ProviderReleaseOutcome.RELEASED,
            ProviderReservationLookupOutcome.NOT_RESERVED,
        )
    if mode == "unknown":
        return (
            ProviderReserveOutcome.UNKNOWN,
            ProviderReleaseOutcome.UNKNOWN,
            ProviderReservationLookupOutcome.UNKNOWN,
        )
    raise ValueError(f"Unsupported mock provider mode: {mode}")
