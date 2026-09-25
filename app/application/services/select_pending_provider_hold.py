from typing import Callable

from app.application.dto.reservations import PendingExternalHoldRecord
from app.application.ports.repositories import UnitOfWork


class SelectPendingProviderHoldService:
    """Selects one pending external HOLD for a future executor invocation."""

    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def execute(self) -> PendingExternalHoldRecord | None:
        async with self._uow_factory() as uow:
            return await uow.reservations.get_next_pending_external_hold()
