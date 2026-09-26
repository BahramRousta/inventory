from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models import InternalStockModel


class SqlAlchemyInternalInventoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def try_hold(self, stock_source_id: UUID, quantity: int) -> bool:
        stmt = (
            update(InternalStockModel)
            .where(
                InternalStockModel.stock_source_id == stock_source_id,
                InternalStockModel.on_hand - InternalStockModel.held >= quantity,
            )
            .values(
                held=InternalStockModel.held + quantity,
            )
            .returning(InternalStockModel.stock_source_id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def release_hold(self, stock_source_id: UUID, quantity: int) -> bool:
        stmt = (
            update(InternalStockModel)
            .where(InternalStockModel.stock_source_id == stock_source_id)
            .where(InternalStockModel.held >= quantity)
            .values(held=InternalStockModel.held - quantity)
            .returning(InternalStockModel.stock_source_id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def consume_hold(self, stock_source_id: UUID, quantity: int) -> bool:
        stmt = (
            update(InternalStockModel)
            .where(InternalStockModel.stock_source_id == stock_source_id)
            .where(InternalStockModel.held >= quantity)
            .where(InternalStockModel.on_hand >= quantity)
            .values(
                held=InternalStockModel.held - quantity,
                on_hand=InternalStockModel.on_hand - quantity,
            )
            .returning(InternalStockModel.stock_source_id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None
