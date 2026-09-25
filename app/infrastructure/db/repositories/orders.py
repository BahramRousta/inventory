from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models import OrderModel


class SqlAlchemyOrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_reservation_id(self, reservation_id: UUID) -> UUID | None:
        return await self._session.scalar(
            select(OrderModel.id).where(OrderModel.reservation_id == reservation_id)
        )

    async def create(self, *, reservation_id: UUID, user_id: str) -> UUID:
        order_id = uuid4()
        self._session.add(
            OrderModel(
                id=order_id,
                reservation_id=reservation_id,
                user_id=user_id,
            )
        )
        await self._session.flush()
        return order_id
