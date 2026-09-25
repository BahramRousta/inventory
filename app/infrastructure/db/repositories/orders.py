from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.dto.reservations import OrderLineRecord
from app.infrastructure.db.models import OrderLineModel, OrderModel


class SqlAlchemyOrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_reservation_id(self, reservation_id: UUID) -> UUID | None:
        return await self._session.scalar(
            select(OrderModel.id).where(OrderModel.reservation_id == reservation_id)
        )

    async def create_with_lines(
        self,
        *,
        reservation_id: UUID,
        user_id: str,
        lines: tuple[OrderLineRecord, ...],
    ) -> UUID:
        existing = await self.get_by_reservation_id(reservation_id)
        if existing is not None:
            return existing

        order_id = uuid4()
        self._session.add(
            OrderModel(
                id=order_id,
                reservation_id=reservation_id,
                user_id=user_id,
            )
        )
        for line in lines:
            self._session.add(
                OrderLineModel(
                    order_id=order_id,
                    product_id=line.product_id,
                    stock_source_id=line.stock_source_id,
                    provider_id=line.provider_id,
                    quantity=line.quantity,
                    provider_allocation_ref=line.provider_allocation_ref,
                )
            )
        await self._session.flush()
        return order_id
