from typing import Callable
from uuid import UUID

from app.application.ports.repositories import UnitOfWork
from app.domain.enums import ProviderKind, ReservationLineStatus


class ProcessReleasingReservationService:
    """Prepare RELEASING reservations for local and external compensation."""

    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def execute(self, reservation_id: UUID) -> bool:
        """Prepare one reservation, used by direct callers and focused tests."""
        async with self._uow_factory() as uow:
            if not await uow.reservations.is_releasing(reservation_id):
                return False
            prepared = await self._prepare_one(uow, reservation_id)
            await uow.commit()
            return prepared

    async def execute_batch(self, *, limit: int) -> tuple[UUID, ...]:
        """Prepare a bounded batch while holding DB row locks until commit.

        RELEASING reservations are selected with FOR UPDATE SKIP LOCKED by the
        repository. Multiple worker processes can therefore prepare disjoint
        batches without introducing another durable claim state. This phase
        performs only local database work; external provider calls happen after
        this transaction has committed.
        """
        async with self._uow_factory() as uow:
            reservation_ids = await uow.reservations.lock_releasing_reservation_ids(
                limit=limit
            )
            if not reservation_ids:
                return ()

            prepared: list[UUID] = []
            for reservation_id in reservation_ids:
                if await self._prepare_one(uow, reservation_id):
                    prepared.append(reservation_id)

            await uow.commit()
            return tuple(prepared)

    async def _prepare_one(self, uow: UnitOfWork, reservation_id: UUID) -> bool:
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

            if not await uow.inventory.release_hold(
                line.stock_source_id,
                line.quantity,
            ):
                raise RuntimeError(
                    f"Could not release internal hold for source {line.stock_source_id}."
                )

            await uow.reservations.mark_line_released(
                reservation_id,
                line.stock_source_id,
            )

        await uow.reservations.cancel_if_all_lines_resolved(reservation_id)
        return True
