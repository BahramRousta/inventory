from datetime import datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from app.application.ports.provider_gateway import (
    ProviderGateway,
    ProviderHoldOutcome,
    ProviderHoldResult,
    ProviderHoldLookupOutcome,
    ProviderHoldLookupResult,
    ProviderReleaseOutcome,
    ProviderReleaseResult,
)


class ExternalHoldHttpGateway(ProviderGateway):
    """Adapter for the assignment's hold-capable HTTP provider contract.

    The configured provider accepts ``POST /holds`` with an idempotency key.
    A 2xx response must include ``hold_ref`` and may include ``expires_at``.
    Only 409 and 422 are treated as proven declines; transport failures and all
    other responses are ambiguous because the remote HOLD may have executed.
    """

    def __init__(self, *, base_url: str, hold_timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(hold_timeout_seconds)

    async def hold(
        self,
        *,
        stock_source_id: UUID,
        quantity: int,
        hold_key: str,
        expires_at: datetime,
    ) -> ProviderHoldResult:
        payload = {
            "stock_source_id": str(stock_source_id),
            "quantity": quantity,
            "operation_key": hold_key,
            "expires_at": expires_at.isoformat(),
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/holds",
                    json=payload,
                    headers={"Idempotency-Key": hold_key},
                )
        except httpx.RequestError:
            return ProviderHoldResult(
                outcome=ProviderHoldOutcome.UNKNOWN,
                error_code="PROVIDER_TRANSPORT_ERROR",
            )

        if response.status_code in {409, 422}:
            return ProviderHoldResult(
                outcome=ProviderHoldOutcome.DECLINED,
                error_code="PROVIDER_HOLD_DECLINED",
            )
        if not response.is_success:
            return ProviderHoldResult(
                outcome=ProviderHoldOutcome.UNKNOWN,
                error_code="PROVIDER_RESPONSE_UNRESOLVED",
            )

        try:
            body: dict[str, Any] = response.json()
            external_hold_ref = body["hold_ref"]
            if not isinstance(external_hold_ref, str) or not external_hold_ref:
                raise ValueError("hold_ref is missing")
            external_expires_at = _parse_optional_timestamp(body.get("expires_at"))
        except (TypeError, ValueError, KeyError):
            return ProviderHoldResult(
                outcome=ProviderHoldOutcome.UNKNOWN,
                error_code="PROVIDER_INVALID_SUCCESS_RESPONSE",
            )

        return ProviderHoldResult(
            outcome=ProviderHoldOutcome.HELD,
            external_hold_ref=external_hold_ref,
            external_expires_at=external_expires_at,
        )

    async def release(
        self,
        *,
        stock_source_id: UUID,
        external_hold_ref: str,
        release_key: str,
    ) -> ProviderReleaseResult:
        del stock_source_id
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/holds/{quote(external_hold_ref, safe='')}/release",
                    headers={"Idempotency-Key": release_key},
                )
        except httpx.RequestError:
            return ProviderReleaseResult(
                outcome=ProviderReleaseOutcome.UNKNOWN,
                error_code="PROVIDER_TRANSPORT_ERROR",
            )

        if response.is_success:
            return ProviderReleaseResult(outcome=ProviderReleaseOutcome.RELEASED)
        return ProviderReleaseResult(
            outcome=ProviderReleaseOutcome.UNKNOWN,
            error_code="PROVIDER_RELEASE_UNRESOLVED",
        )

    async def get_hold(self, *, hold_key: str) -> ProviderHoldLookupResult:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"{self._base_url}/holds/{quote(hold_key, safe='')}"
                )
        except httpx.RequestError:
            return ProviderHoldLookupResult(
                outcome=ProviderHoldLookupOutcome.UNKNOWN,
                error_code="PROVIDER_TRANSPORT_ERROR",
            )

        if response.status_code == 404:
            return ProviderHoldLookupResult(
                outcome=ProviderHoldLookupOutcome.NOT_HELD
            )
        if not response.is_success:
            return ProviderHoldLookupResult(
                outcome=ProviderHoldLookupOutcome.UNKNOWN,
                error_code="PROVIDER_LOOKUP_UNRESOLVED",
            )
        try:
            body: dict[str, Any] = response.json()
            state = body["status"]
            if state in {"RELEASED", "NOT_HELD"}:
                return ProviderHoldLookupResult(
                    outcome=ProviderHoldLookupOutcome.NOT_HELD
                )
            if state != "HELD":
                raise ValueError("unrecognized hold state")
            external_hold_ref = body["hold_ref"]
            if not isinstance(external_hold_ref, str) or not external_hold_ref:
                raise ValueError("hold_ref is missing")
        except (TypeError, ValueError, KeyError):
            return ProviderHoldLookupResult(
                outcome=ProviderHoldLookupOutcome.UNKNOWN,
                error_code="PROVIDER_INVALID_LOOKUP_RESPONSE",
            )
        return ProviderHoldLookupResult(
            outcome=ProviderHoldLookupOutcome.HELD,
            external_hold_ref=external_hold_ref,
        )


def _parse_optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expires_at must be a string")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
