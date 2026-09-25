from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.dto.reservations import (
    ReservationIdentityRecord,
    ReservationItemCommand,
    ReservationLineResult,
)
from app.application.errors import PersistenceConflict
from app.domain.enums import ReservationLineStatus, ReservationStatus
from app.infrastructure.db.models import ReservationLineModel, ReservationModel, StockSourceModel


class SqlAlchemyReservationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_idempotency_key(
        self, user_id: str, idempotency_key: str
    ) -> ReservationIdentityRecord | None:
        row = await self._session.scalar(
            select(ReservationModel).where(
                ReservationModel.user_id == user_id,
                ReservationModel.idempotency_key == idempotency_key,
            )
        )
        if row is None:
            return None
        return ReservationIdentityRecord(
            reservation_id=row.id,
            user_id=row.user_id,
            idempotency_key=row.idempotency_key,
            status=row.status,
            expires_at=_as_utc(row.expires_at),
        )

    async def create(
        self,
        *,
        reservation_id: UUID,
        user_id: str,
        idempotency_key: str,
        expires_at: datetime,
        status: ReservationStatus,
    ) -> None:
        self._session.add(
            ReservationModel(
                id=reservation_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                expires_at=expires_at,
                status=status,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise PersistenceConflict from exc

    async def add_held_line(self, reservation_id: UUID, item: ReservationItemCommand) -> None:
        self._session.add(
            ReservationLineModel(
                reservation_id=reservation_id,
                stock_source_id=item.stock_source_id,
                quantity=item.quantity,
                status=ReservationLineStatus.HELD,
                held_at=datetime.now(timezone.utc),
            )
        )
        await self._session.flush()

    async def set_status(self, reservation_id: UUID, status: ReservationStatus) -> None:
        await self._session.execute(
            update(ReservationModel)
            .where(ReservationModel.id == reservation_id)
            .values(status=status)
        )
        await self._session.flush()

    async def get_lines(self, reservation_id: UUID) -> tuple[ReservationLineResult, ...]:
        rows = (await self._session.execute(
            select(ReservationLineModel, StockSourceModel.product_id)
            .join(StockSourceModel, StockSourceModel.id == ReservationLineModel.stock_source_id)
            .where(ReservationLineModel.reservation_id == reservation_id)
            .order_by(ReservationLineModel.stock_source_id)
        )).all()
        return tuple(
            ReservationLineResult(
                product_id=product_id,
                stock_source_id=line.stock_source_id,
                quantity=line.quantity,
                status=line.status,
            )
            for line, product_id in rows
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
