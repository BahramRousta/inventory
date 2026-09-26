from typing import Callable
from uuid import UUID

from app.application.ports.repositories import UnitOfWork


class ExpireReservingReservationService:
    """Moves one expired, in-progress reservation into RELEASING."""

    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def execute_batch(self, *, limit: int) -> tuple[UUID, ...]:
        async with self._uow_factory() as uow:
            reservation_ids = await uow.reservations.claim_expired_reserving_reservations(
                limit=limit
            )
            await uow.commit()
            return reservation_ids
