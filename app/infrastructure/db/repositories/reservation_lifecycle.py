from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import case, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.dto.reservations import ReservationIdentityRecord
from app.domain.enums import ReservationLineStatus, ReservationStatus
from app.infrastructure.db.models import ReservationLineModel, ReservationModel
from app.infrastructure.db.repositories.reservations import SqlAlchemyReservationRepository


class SqlAlchemyLifecycleReservationRepository(SqlAlchemyReservationRepository):
    """Lifecycle extensions kept separate from provider-work persistence."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_by_id(self, reservation_id: UUID) -> ReservationIdentityRecord | None:
        row = await self._session.scalar(
            select(ReservationModel).where(ReservationModel.id == reservation_id)
        )
        if row is None:
            return None
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        else:
            expires_at = expires_at.astimezone(timezone.utc)
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = created_at.astimezone(timezone.utc)
        self.record = ReservationIdentityRecord(
            reservation_id=row.id,
            user_id=row.user_id,
            idempotency_key=row.idempotency_key,
            request_fingerprint=row.request_fingerprint,
            status=row.status,
            created_at=created_at,
            expires_at=expires_at,
        )
        return self.record

    async def begin_confirming_if_active(self, reservation_id: UUID) -> bool:
        result = await self._session.execute(
            update(ReservationModel)
            .where(
                ReservationModel.id == reservation_id,
                ReservationModel.status == ReservationStatus.ACTIVE,
                ReservationModel.expires_at > func.now(),
            )
            .values(status=ReservationStatus.CONFIRMING)
        )
        await self._session.flush()
        return result.rowcount == 1

    async def mark_line_confirmed(self, reservation_id: UUID, stock_source_id: UUID) -> None:
        await self._session.execute(
            update(ReservationLineModel)
            .where(
                ReservationLineModel.reservation_id == reservation_id,
                ReservationLineModel.stock_source_id == stock_source_id,
                ReservationLineModel.status == ReservationLineStatus.HELD,
            )
            .values(
                status=ReservationLineStatus.CONFIRMED,
                committed_at=datetime.now(timezone.utc),
            )
        )
        await self._session.flush()

    async def confirm_if_all_lines_confirmed(self, reservation_id: UUID) -> bool:
        has_non_confirmed_line = exists(
            select(1).where(
                ReservationLineModel.reservation_id == reservation_id,
                ReservationLineModel.status != ReservationLineStatus.CONFIRMED,
            )
        )
        result = await self._session.execute(
            update(ReservationModel)
            .where(
                ReservationModel.id == reservation_id,
                ReservationModel.status == ReservationStatus.CONFIRMING,
                ~has_non_confirmed_line,
            )
            .values(
                status=ReservationStatus.CONFIRMED,
                confirmed_at=datetime.now(timezone.utc),
            )
        )
        await self._session.flush()
        return result.rowcount == 1

    async def begin_releasing(self, reservation_id: UUID, reason: str) -> bool:
        result = await self._session.execute(
            update(ReservationModel)
            .where(
                ReservationModel.id == reservation_id,
                ReservationModel.status.in_(
                    (ReservationStatus.RESERVING, ReservationStatus.ACTIVE)
                ),
            )
            .values(status=ReservationStatus.RELEASING, release_reason=reason)
        )
        await self._session.flush()
        return result.rowcount == 1

    async def cancel_if_all_lines_resolved(self, reservation_id: UUID) -> bool:
        has_unresolved_line = exists(
            select(1).where(
                ReservationLineModel.reservation_id == reservation_id,
                ReservationLineModel.status.not_in(
                    (ReservationLineStatus.FAILED, ReservationLineStatus.RELEASED)
                ),
            )
        )
        result = await self._session.execute(
            update(ReservationModel)
            .where(
                ReservationModel.id == reservation_id,
                ReservationModel.status == ReservationStatus.RELEASING,
                ~has_unresolved_line,
            )
            .values(
                status=case(
                    (
                        ReservationModel.release_reason == "EXPIRED",
                        ReservationStatus.EXPIRED,
                    ),
                    else_=ReservationStatus.CANCELLED,
                )
            )
        )
        await self._session.flush()
        return result.rowcount == 1
