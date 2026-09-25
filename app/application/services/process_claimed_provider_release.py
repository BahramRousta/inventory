from typing import Callable

from app.application.dto.reservations import ClaimedExternalReleaseRecord
from app.application.ports.provider_gateway import (
    ProviderGatewayRegistry,
    ProviderReleaseOutcome,
    ProviderReleaseResult,
)
from app.application.ports.repositories import UnitOfWork
from app.domain.enums import ReservationLineStatus


class ProcessClaimedProviderReleaseService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        provider_gateways: ProviderGatewayRegistry,
    ) -> None:
        self._uow_factory = uow_factory
        self._provider_gateways = provider_gateways

    async def execute(self, work: ClaimedExternalReleaseRecord) -> bool:
        async with self._uow_factory() as uow:
            if not await uow.reservations.is_external_release_claim_owned(
                work.reservation_id, work.stock_source_id, work.claim_token
            ):
                return False
        gateway = self._provider_gateways.get_reservation_provider(work.provider_id)
        result = await self._attempt(gateway, work)
        status = (
            ReservationLineStatus.RELEASED
            if result.outcome == ProviderReleaseOutcome.RELEASED
            else ReservationLineStatus.RELEASE_UNKNOWN
        )
        async with self._uow_factory() as uow:
            persisted = await uow.reservations.record_external_release_result(
                reservation_id=work.reservation_id,
                stock_source_id=work.stock_source_id,
                claim_token=work.claim_token,
                status=status,
            )
            if persisted:
                await uow.reservations.cancel_if_all_lines_resolved(work.reservation_id)
                await uow.commit()
            return persisted

    @staticmethod
    async def _attempt(gateway, work: ClaimedExternalReleaseRecord) -> ProviderReleaseResult:
        if gateway is None:
            return ProviderReleaseResult(ProviderReleaseOutcome.UNKNOWN)
        try:
            return await gateway.release(
                stock_source_id=work.stock_source_id,
                external_hold_ref=work.external_hold_ref,
                release_key=f"{work.reservation_id}:{work.stock_source_id}:RELEASE",
            )
        except Exception:
            return ProviderReleaseResult(ProviderReleaseOutcome.UNKNOWN)
