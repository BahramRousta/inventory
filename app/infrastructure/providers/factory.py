from app.application.ports.provider_gateway import (
    ProviderGatewayRegistry,
    ProviderHoldLookupOutcome,
    ProviderHoldOutcome,
    ProviderRegistry,
    ProviderReleaseOutcome,
)
from app.bootstrap.config import Settings
from app.infrastructure.providers.mock import (
    MockAvailabilityProviderGateway,
    MockReservationProviderGateway,
)


class ProviderGatewayFactory:
    """Build the assignment provider registry from simple configuration."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def create_registry(self) -> ProviderGatewayRegistry:
        reservation_providers = {}
        availability_providers = {}

        if self._settings.external_provider_id is not None:
            hold_outcome, release_outcome, lookup_outcome = _mode_outcomes(
                self._settings.mock_provider_mode
            )
            reservation_providers[self._settings.external_provider_id] = (
                MockReservationProviderGateway(
                    hold_outcome=hold_outcome,
                    release_outcome=release_outcome,
                    lookup_outcome=lookup_outcome,
                )
            )

        if self._settings.query_only_provider_id is not None:
            availability_providers[self._settings.query_only_provider_id] = (
                MockAvailabilityProviderGateway(
                    available_quantity=self._settings.mock_provider_available_quantity
                )
            )

        return ProviderRegistry(
            reservation_providers=reservation_providers,
            availability_providers=availability_providers,
        )


def _mode_outcomes(mode: str):
    if mode == "success":
        return (
            ProviderHoldOutcome.HELD,
            ProviderReleaseOutcome.RELEASED,
            ProviderHoldLookupOutcome.HELD,
        )
    if mode == "decline":
        return (
            ProviderHoldOutcome.DECLINED,
            ProviderReleaseOutcome.RELEASED,
            ProviderHoldLookupOutcome.NOT_HELD,
        )
    if mode == "unknown":
        return (
            ProviderHoldOutcome.UNKNOWN,
            ProviderReleaseOutcome.UNKNOWN,
            ProviderHoldLookupOutcome.UNKNOWN,
        )
    raise ValueError(f"Unsupported mock provider mode: {mode}")
