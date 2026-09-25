from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.dto.reservations import PaymentEventRecord
from app.application.errors import PersistenceConflict
from app.infrastructure.db.models import PaymentEventModel


class SqlAlchemyPaymentEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, event_id: UUID) -> PaymentEventRecord | None:
        row = await self._session.get(PaymentEventModel, event_id)
        if row is None:
            return None
        return PaymentEventRecord(
            event_id=row.event_id,
            reservation_id=row.reservation_id,
            user_id=row.user_id,
            outcome=row.outcome,
            payload_hash=row.payload_hash,
        )

    async def create(self, event: PaymentEventRecord) -> None:
        self._session.add(
            PaymentEventModel(
                event_id=event.event_id,
                reservation_id=event.reservation_id,
                user_id=event.user_id,
                outcome=event.outcome,
                payload_hash=event.payload_hash,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise PersistenceConflict from exc
