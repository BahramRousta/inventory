from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.db.repositories.inventory import SqlAlchemyInternalInventoryRepository
from app.infrastructure.db.repositories.reservations import SqlAlchemyReservationRepository
from app.infrastructure.db.repositories.stock_sources import SqlAlchemyStockSourceRepository


class SqlAlchemyUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.session: AsyncSession | None = None

    async def __aenter__(self) -> "SqlAlchemyUnitOfWork":
        self.session = self._session_factory()
        self.stock_sources = SqlAlchemyStockSourceRepository(self.session)
        self.reservations = SqlAlchemyReservationRepository(self.session)
        self.inventory = SqlAlchemyInternalInventoryRepository(self.session)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        assert self.session is not None
        if exc is not None:
            await self.session.rollback()
        await self.session.close()

    async def commit(self) -> None:
        assert self.session is not None
        await self.session.commit()

    async def rollback(self) -> None:
        assert self.session is not None
        await self.session.rollback()
