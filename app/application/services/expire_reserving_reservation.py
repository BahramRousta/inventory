from typing import Callable
from uuid import UUID

from app.application.ports.repositories import UnitOfWork


class ExpireReservingReservationService:
    """Moves one expired, in-progress reservation into RELEASING."""

    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def execute_one(self) -> UUID | None:
        async with self._uow_factory() as uow:
            reservation_id = (
                await uow.reservations.claim_next_expired_reserving_reservation()
            )
            await uow.commit()
            return reservation_id
