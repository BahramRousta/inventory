from typing import Callable

from app.application.dto.reservations import ClaimedExternalHoldRecord
from app.application.ports.provider_gateway import (
    ProviderRegistryProtocol,
    ProviderReserveOutcome,
    ProviderReserveResult,
)
from app.application.ports.repositories import UnitOfWork
from app.domain.enums import ReservationLineStatus


class ProcessPendingProviderHoldService:
    """Processes one claimed external reservation line outside DB transactions."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        providers: ProviderRegistryProtocol,
    ) -> None:
        self._uow_factory = uow_factory
        self._providers = providers

    async def execute(self, work: ClaimedExternalHoldRecord) -> bool:
        reservation_key = f"{work.reservation_id}:{work.stock_source_id}:RESERVE"

        # this helped us to be sure other worker don't process and changed it
        async with self._uow_factory() as uow:
            if not await uow.reservations.is_external_hold_claim_owned(
                work.reservation_id,
                work.stock_source_id,
                work.claim_token,
            ):
                return False

        provider = self._providers.get(work.provider_id)
        result: ProviderReserveResult = await self._reserve(
            provider=provider,
            work=work,
            reservation_key=reservation_key,
        )
        return await self._persist_result(work, result)

    async def _reserve(
        self,
        *,
        provider,
        work: ClaimedExternalHoldRecord,
        reservation_key: str,
    ) -> ProviderReserveResult:
        if provider is None:
            return ProviderReserveResult(
                outcome=ProviderReserveOutcome.DECLINED,
                error_code="PROVIDER_NOT_CONFIGURED",
            )

        try:
            return await provider.reserve(
                stock_source_id=work.stock_source_id,
                quantity=work.quantity,
                reservation_key=reservation_key,
                expires_at=work.expires_at,
            )
        except Exception:
            return ProviderReserveResult(
                outcome=ProviderReserveOutcome.UNKNOWN,
                error_code="PROVIDER_RESERVE_EXCEPTION",
            )

    async def _persist_result(
        self,
        work: ClaimedExternalHoldRecord,
        result: ProviderReserveResult,
    ) -> bool:
        line_status = {
            ProviderReserveOutcome.RESERVED: ReservationLineStatus.HELD,
            ProviderReserveOutcome.DECLINED: ReservationLineStatus.FAILED,
            ProviderReserveOutcome.UNKNOWN: ReservationLineStatus.HOLD_UNKNOWN,
        }[result.outcome]

        async with self._uow_factory() as uow:
            persisted = await uow.reservations.record_external_hold_result(
                reservation_id=work.reservation_id,
                stock_source_id=work.stock_source_id,
                claim_token=work.claim_token,
                status=line_status,
                external_hold_ref=result.external_ref,
            )
            if not persisted:
                return False

            # we check if all sibling lines are processed before finalizing the reservation itself
            if line_status == ReservationLineStatus.HELD:
                await uow.reservations.activate_if_all_lines_held(work.reservation_id)
            elif line_status == ReservationLineStatus.FAILED:
                await uow.reservations.begin_releasing_if_reserving(work.reservation_id)

            await uow.commit()
            return True
