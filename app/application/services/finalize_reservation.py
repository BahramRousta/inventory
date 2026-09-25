from uuid import UUID

from app.application.dto.reservations import OrderLineRecord
from app.application.errors import ReservationStateConflict
from app.application.ports.provider_gateway import ProviderGatewayRegistry
from app.application.ports.repositories import UnitOfWork
from app.domain.enums import ProviderKind, ReservationLineStatus


async def finalize_confirming_reservation(
    uow: UnitOfWork,
    *,
    reservation_id: UUID,
    user_id: str,
    provider_gateways: ProviderGatewayRegistry,
) -> UUID:
    lines = await uow.reservations.get_lines(reservation_id)
    if not lines:
        raise ReservationStateConflict("Reservation has no lines.")

    sources = await uow.stock_sources.get_many(
        tuple(line.stock_source_id for line in lines)
    )
    order_lines: list[OrderLineRecord] = []

    for line in lines:
        if line.status != ReservationLineStatus.HELD:
            raise ReservationStateConflict(
                f"Line {line.stock_source_id} is not held."
            )
        source = sources.get(line.stock_source_id)
        if source is None:
            raise ReservationStateConflict(
                f"Stock source {line.stock_source_id} disappeared."
            )

        if source.provider_kind == ProviderKind.INTERNAL:
            consumed = await uow.inventory.consume_hold(
                line.stock_source_id, line.quantity
            )
            if not consumed:
                raise ReservationStateConflict(
                    f"Internal hold for {line.stock_source_id} cannot be consumed."
                )
            provider_ref = None
        else:
            gateway = provider_gateways.get(source.provider_id)
            if gateway is None:
                raise ReservationStateConflict(
                    f"Provider {source.provider_id} has no configured gateway."
                )
            if not gateway.capabilities.hold_is_final_allocation:
                raise ReservationStateConflict(
                    f"Provider {source.provider_id} does not declare HOLD as final allocation."
                )
            provider_ref = await uow.reservations.get_external_hold_ref(
                reservation_id, line.stock_source_id
            )
            if not provider_ref:
                raise ReservationStateConflict(
                    f"External line {line.stock_source_id} has no durable allocation reference."
                )

        await uow.reservations.mark_line_confirmed(
            reservation_id, line.stock_source_id
        )
        order_lines.append(
            OrderLineRecord(
                product_id=line.product_id,
                stock_source_id=line.stock_source_id,
                provider_id=source.provider_id,
                quantity=line.quantity,
                provider_allocation_ref=provider_ref,
            )
        )

    if not await uow.reservations.confirm_if_all_lines_confirmed(reservation_id):
        raise ReservationStateConflict(
            f"Reservation {reservation_id} could not be finalized."
        )

    return await uow.orders.create_with_lines(
        reservation_id=reservation_id,
        user_id=user_id,
        lines=tuple(order_lines),
    )
