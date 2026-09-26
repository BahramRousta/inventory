from typing import Callable

from app.application.dto.reservations import ClaimedExternalHoldRecord
from app.application.ports.repositories import UnitOfWork


class ClaimPendingProviderHoldsService:
    """Durably claims one bounded batch of external HOLD work."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        batch_size: int,
        lease_seconds: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds

    async def execute(self) -> tuple[ClaimedExternalHoldRecord, ...]:
        async with self._uow_factory() as uow:
            claimed = await uow.reservations.claim_pending_external_holds(
                limit=self._batch_size,
                lease_seconds=self._lease_seconds,
            )
            await uow.commit()
            return claimed
