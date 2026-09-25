from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.dto.reservations import StockSourceRecord
from app.domain.enums import ProviderCapability
from app.infrastructure.db.models import StockSourceModel


class SqlAlchemyStockSourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_many(self, source_ids: tuple[UUID, ...]) -> dict[UUID, StockSourceRecord]:
        if not source_ids:
            return {}
        rows = (await self._session.scalars(
            select(StockSourceModel).where(StockSourceModel.id.in_(source_ids))
        )).all()
        result: dict[UUID, StockSourceRecord] = {}
        for row in rows:
            result[row.id] = StockSourceRecord(
                stock_source_id=row.id,
                product_id=row.product_id,
                provider_id=row.provider_id,
                provider_kind=row.provider.kind,
                provider_enabled=row.provider.enabled,
                source_enabled=row.enabled,
            )
        return result
