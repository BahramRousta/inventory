from app.application.ports.provider_gateway import ProviderCapabilities


# Static contract of the assignment HTTP provider adapter.
# Runtime values such as URL, API key, provider ID and timeouts come from env.
EXTERNAL_HOLD_HTTP_CAPABILITIES = ProviderCapabilities(
    supports_check=False,
    supports_hold=True,
    supports_release=True,
    supports_get_hold=True,
    hold_is_final_allocation=True,
)
