from app.application.ports.provider_gateway import InMemoryProviderGatewayRegistry
from app.bootstrap.config import Settings
from app.infrastructure.providers.external_hold_http import ExternalHoldHttpGateway


class ProviderGatewayFactory:
    """Build provider adapters from deployment configuration.

    Provider capabilities belong to the adapter implementation; endpoints and
    credentials come from environment-backed Settings.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def create_registry(self) -> InMemoryProviderGatewayRegistry:
        settings = self._settings
        if settings.external_provider_id is None or settings.external_provider_base_url is None:
            return InMemoryProviderGatewayRegistry()

        gateway = ExternalHoldHttpGateway(
            base_url=settings.external_provider_base_url,
            hold_timeout_seconds=settings.external_provider_hold_timeout_seconds,
            api_key=settings.external_provider_api_key,
        )
        return InMemoryProviderGatewayRegistry({settings.external_provider_id: gateway})
