from enum import StrEnum
from typing import Callable

from app.application.ports.repositories import UnitOfWork
from app.application.services.process_pending_provider_hold import (
    ProcessPendingProviderHoldService,
)
from app.application.services.process_releasing_reservation import (
    ProcessReleasingReservationService,
)
from app.application.services.reconcile_provider_work import ReconcileProviderWorkService
from app.application.services.select_pending_provider_hold import (
    SelectPendingProviderHoldService,
)


class ProviderWorkerTickResult(StrEnum):
    EXPIRED_AND_RELEASING = "EXPIRED_AND_RELEASING"
    RELEASING = "RELEASING"
    HOLD = "HOLD"
    RECONCILIATION = "RECONCILIATION"
    IDLE = "IDLE"


class ProviderWorkerTickService:
    """Runs at most one unit of provider workflow work per tick."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        select_pending_hold: SelectPendingProviderHoldService,
        process_pending_hold: ProcessPendingProviderHoldService,
        process_releasing: ProcessReleasingReservationService,
        reconcile_work: ReconcileProviderWorkService,
    ) -> None:
        self._uow_factory = uow_factory
        self._select_pending_hold = select_pending_hold
        self._process_pending_hold = process_pending_hold
        self._process_releasing = process_releasing
        self._reconcile_work = reconcile_work

    async def execute_once(self) -> ProviderWorkerTickResult:
        async with self._uow_factory() as uow:
            expired_reservation_id = (
                await uow.reservations.claim_next_expired_reserving_reservation()
            )
            await uow.commit()
        if expired_reservation_id is not None:
            await self._process_releasing.execute(expired_reservation_id)
            return ProviderWorkerTickResult.EXPIRED_AND_RELEASING

        async with self._uow_factory() as uow:
            releasing_reservation_id = (
                await uow.reservations.get_next_releasing_reservation_id()
            )
        if releasing_reservation_id is not None:
            await self._process_releasing.execute(releasing_reservation_id)
            return ProviderWorkerTickResult.RELEASING

        pending_hold = await self._select_pending_hold.execute()
        if pending_hold is not None:
            await self._process_pending_hold.execute(pending_hold)
            return ProviderWorkerTickResult.HOLD

        if await self._reconcile_work.execute_one():
            return ProviderWorkerTickResult.RECONCILIATION
        return ProviderWorkerTickResult.IDLE
