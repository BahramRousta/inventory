from typing import Callable

from app.application.dto.reservations import (
    PendingExternalHoldRecord,
    PendingExternalReleaseRecord,
)
from app.application.ports.provider_gateway import (
    ProviderGatewayRegistry,
    ProviderHoldLookupOutcome,
    ProviderHoldLookupResult,
)
from app.application.ports.provider_work_lock import ProviderWorkLock
from app.application.ports.repositories import UnitOfWork
from app.domain.enums import ReservationLineStatus


class ReconcileProviderWorkService:
    """Reconciles one ambiguous HOLD or RELEASE without creating new work."""

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

    async def execute_one(self) -> bool:
        async with self._uow_factory() as uow:
            hold = await uow.reservations.get_next_unknown_external_hold()
            release = (
                None if hold is not None else await uow.reservations.get_next_unknown_external_release()
            )
        if hold is not None:
            return await self._reconcile_hold(hold)
        if release is not None:
            return await self._reconcile_release(release)
        return False

    async def _reconcile_hold(self, work: PendingExternalHoldRecord) -> bool:
        hold_key = f"{work.reservation_id}:{work.stock_source_id}:HOLD"
        async with self._work_lock.try_acquire(hold_key) as acquired:
            if not acquired:
                return False
            result = await self._lookup(work.provider_id, hold_key)
            if result.outcome == ProviderHoldLookupOutcome.UNKNOWN:
                return True
            line_status = (
                ReservationLineStatus.HELD
                if result.outcome == ProviderHoldLookupOutcome.HELD
                else ReservationLineStatus.FAILED
            )
            async with self._uow_factory() as uow:
                await uow.reservations.reconcile_external_hold_result(
                    reservation_id=work.reservation_id,
                    stock_source_id=work.stock_source_id,
                    status=line_status,
                    external_hold_ref=result.external_hold_ref,
                )
                if line_status == ReservationLineStatus.HELD:
                    if await uow.reservations.is_releasing(work.reservation_id):
                        await uow.reservations.claim_line_for_release(
                            work.reservation_id, work.stock_source_id
                        )
                    else:
                        await uow.reservations.activate_if_all_lines_held(
                            work.reservation_id
                        )
                else:
                    await uow.reservations.begin_releasing_if_reserving(
                        work.reservation_id
                    )
                    if await uow.reservations.is_releasing(work.reservation_id):
                        await uow.reservations.cancel_if_all_lines_resolved(
                            work.reservation_id
                        )
                await uow.commit()
            return True

    async def _reconcile_release(self, work: PendingExternalReleaseRecord) -> bool:
        hold_key = f"{work.reservation_id}:{work.stock_source_id}:HOLD"
        release_key = f"{work.reservation_id}:{work.stock_source_id}:RELEASE"
        async with self._work_lock.try_acquire(release_key) as acquired:
            if not acquired:
                return False
            result = await self._lookup(work.provider_id, hold_key)
            if result.outcome == ProviderHoldLookupOutcome.UNKNOWN:
                return True
            async with self._uow_factory() as uow:
                if result.outcome == ProviderHoldLookupOutcome.NOT_HELD:
                    await uow.reservations.mark_unknown_release_released(
                        work.reservation_id, work.stock_source_id
                    )
                    await uow.reservations.cancel_if_all_lines_resolved(work.reservation_id)
                else:
                    await uow.reservations.restore_release_pending_from_unknown(
                        work.reservation_id, work.stock_source_id
                    )
                await uow.commit()
            return True

    async def _lookup(
        self, provider_id, hold_key: str
    ) -> ProviderHoldLookupResult:
        gateway = self._provider_gateways.get(provider_id)
        if gateway is None:
            return ProviderHoldLookupResult(
                outcome=ProviderHoldLookupOutcome.UNKNOWN,
                error_code="PROVIDER_GATEWAY_UNAVAILABLE",
            )
        try:
            return await gateway.get_hold(hold_key=hold_key)
        except Exception:
            return ProviderHoldLookupResult(
                outcome=ProviderHoldLookupOutcome.UNKNOWN,
                error_code="PROVIDER_GATEWAY_EXCEPTION",
            )
