from typing import Callable
from uuid import UUID

from app.application.ports.repositories import UnitOfWork
from app.domain.enums import ProviderKind, ReservationLineStatus


class ProcessReleasingReservationService:
    """Prepares one RELEASING reservation for local and external compensation."""

    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def execute(self, reservation_id: UUID) -> bool:
        async with self._uow_factory() as uow:
            if not await uow.reservations.is_releasing(reservation_id):
                return False
            lines = await uow.reservations.get_lines(reservation_id)
            if not lines:
                return False
            sources = await uow.stock_sources.get_many(
                tuple(line.stock_source_id for line in lines)
            )
            for line in lines:
                if line.status == ReservationLineStatus.HOLD_PENDING:
                    await uow.reservations.fail_pending_hold_for_release(
                        reservation_id, line.stock_source_id
                    )
                    continue
                if line.status != ReservationLineStatus.HELD:
                    continue
                if not await uow.reservations.claim_line_for_release(
                    reservation_id, line.stock_source_id
                ):
                    continue
                if sources[line.stock_source_id].provider_kind == ProviderKind.EXTERNAL:
                    continue
                if not await uow.inventory.release_hold(line.stock_source_id, line.quantity):
                    raise RuntimeError(
                        f"Could not release internal hold for source {line.stock_source_id}."
                    )
                await uow.reservations.mark_line_released(reservation_id, line.stock_source_id)
            await uow.reservations.cancel_if_all_lines_resolved(reservation_id)
            await uow.commit()
            return True
