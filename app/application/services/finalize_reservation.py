from uuid import UUID

from app.application.errors import ReservationStateConflict
from app.application.ports.repositories import UnitOfWork
from app.domain.enums import ProviderKind, ReservationLineStatus


async def finalize_confirming_reservation(
    uow: UnitOfWork,
    *,
    reservation_id: UUID,
    user_id: str,
) -> UUID:
    lines = await uow.reservations.get_lines(reservation_id)
    if not lines:
        raise ReservationStateConflict("Reservation has no lines.")

    sources = await uow.stock_sources.get_many(tuple(line.stock_source_id for line in lines))

    for line in lines:
        if line.status != ReservationLineStatus.HELD:
            raise ReservationStateConflict(f"Line {line.stock_source_id} is not held.")
        source = sources.get(line.stock_source_id)
        if source is None:
            raise ReservationStateConflict(f"Stock source {line.stock_source_id} disappeared.")

        if source.provider_kind == ProviderKind.INTERNAL:
            consumed = await uow.inventory.consume_hold(line.stock_source_id, line.quantity)
            if not consumed:
                raise ReservationStateConflict(
                    f"Internal hold for {line.stock_source_id} cannot be consumed."
                )
        await uow.reservations.mark_line_confirmed(reservation_id, line.stock_source_id)

    if not await uow.reservations.confirm_if_all_lines_confirmed(reservation_id):
        raise ReservationStateConflict(f"Reservation {reservation_id} could not be finalized.")

    return await uow.orders.create(
        reservation_id=reservation_id,
        user_id=user_id,
    )
