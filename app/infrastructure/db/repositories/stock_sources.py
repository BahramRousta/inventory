from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.dto.reservations import StockSourceRecord
from app.domain.enums import ProviderCapability, ProviderKind
from app.infrastructure.db.models import InventoryProviderModel, StockSourceModel


class SqlAlchemyStockSourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_many(self, source_ids: tuple[UUID, ...]) -> dict[UUID, StockSourceRecord]:
        if not source_ids:
            return {}
        statement = (
            select(StockSourceModel, InventoryProviderModel)
            .join(
                InventoryProviderModel,
                StockSourceModel.provider_id == InventoryProviderModel.id,
            )
            .where(StockSourceModel.id.in_(source_ids))
        )
        rows = (await self._session.execute(statement)).all()

        result: dict[UUID, StockSourceRecord] = {}
        for source, provider in rows:
            capabilities = set(provider.capabilities or [])
            reservation_supported = (
                provider.kind == ProviderKind.INTERNAL
                or ProviderCapability.HOLD.value in capabilities
            )
            result[source.id] = StockSourceRecord(
                stock_source_id=source.id,
                product_id=source.product_id,
                provider_id=source.provider_id,
                provider_kind=provider.kind,
                provider_enabled=provider.enabled,
                source_enabled=source.enabled,
                reservation_supported=reservation_supported,
            )
        return result
