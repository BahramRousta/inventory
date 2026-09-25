from typing import Callable

from app.application.dto.reservations import PendingExternalHoldRecord
from app.application.ports.provider_gateway import (
    ProviderGatewayRegistry,
    ProviderHoldOutcome,
    ProviderHoldResult,
)
from app.application.ports.provider_work_lock import ProviderWorkLock
from app.application.ports.repositories import UnitOfWork
from app.domain.enums import ReservationLineStatus


class ProcessPendingProviderHoldService:
    """Processes exactly one selected external HOLD outside database transactions."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        provider_gateways: ProviderGatewayRegistry,
        work_lock: ProviderWorkLock,
    ) -> None:
        self._uow_factory = uow_factory
        self._provider_gateways = provider_gateways
        self._work_lock = work_lock

    async def execute(self, work: PendingExternalHoldRecord) -> bool:
        hold_key = f"{work.reservation_id}:{work.stock_source_id}:HOLD"
        async with self._work_lock.try_acquire(hold_key) as acquired:
            if not acquired:
                return False

            async with self._uow_factory() as uow:
                if not await uow.reservations.is_external_hold_pending(
                    work.reservation_id, work.stock_source_id
                ):
                    return False

            gateway = self._provider_gateways.get(work.provider_id)
            result = await self._attempt_hold(
                gateway=gateway,
                work=work,
                hold_key=hold_key,
            )
            await self._persist_result(work, result)
            return True

    async def _attempt_hold(
        self,
        *,
        gateway,
        work: PendingExternalHoldRecord,
        hold_key: str,
    ) -> ProviderHoldResult:
        if gateway is None:
            return ProviderHoldResult(
                outcome=ProviderHoldOutcome.UNKNOWN,
                error_code="PROVIDER_GATEWAY_UNAVAILABLE",
            )
        try:
            return await gateway.hold(
                stock_source_id=work.stock_source_id,
                quantity=work.quantity,
                hold_key=hold_key,
                expires_at=work.expires_at,
            )
        except Exception:
            return ProviderHoldResult(
                outcome=ProviderHoldOutcome.UNKNOWN,
                error_code="PROVIDER_GATEWAY_EXCEPTION",
            )

    async def _persist_result(
        self,
        work: PendingExternalHoldRecord,
        result: ProviderHoldResult,
    ) -> None:
        line_status = {
            ProviderHoldOutcome.HELD: ReservationLineStatus.HELD,
            ProviderHoldOutcome.DECLINED: ReservationLineStatus.FAILED,
            ProviderHoldOutcome.UNKNOWN: ReservationLineStatus.HOLD_UNKNOWN,
        }[result.outcome]
        async with self._uow_factory() as uow:
            await uow.reservations.record_external_hold_result(
                reservation_id=work.reservation_id,
                stock_source_id=work.stock_source_id,
                status=line_status,
                external_hold_ref=result.external_hold_ref,
            )
            if line_status == ReservationLineStatus.HELD:
                await uow.reservations.activate_if_all_lines_held(work.reservation_id)
            elif line_status == ReservationLineStatus.FAILED:
                await uow.reservations.begin_releasing_if_reserving(work.reservation_id)
            await uow.commit()
