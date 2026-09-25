from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.dto.reservations import (
    ExternalReleaseRecord,
    PendingExternalHoldRecord,
    PendingExternalReleaseRecord,
    ReservationIdentityRecord,
    ReservationItemCommand,
    ReservationLineResult,
)
from app.application.errors import PersistenceConflict
from app.domain.enums import ProviderKind, ReservationLineStatus, ReservationStatus
from app.infrastructure.db.models import (
    InventoryProviderModel,
    ReservationLineModel,
    ReservationModel,
    StockSourceModel,
)


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

    async def add_line(
        self,
        reservation_id: UUID,
        item: ReservationItemCommand,
        status: ReservationLineStatus,
    ) -> None:
        self._session.add(
            ReservationLineModel(
                reservation_id=reservation_id,
                stock_source_id=item.stock_source_id,
                quantity=item.quantity,
                status=status,
                held_at=(
                    datetime.now(timezone.utc)
                    if status == ReservationLineStatus.HELD
                    else None
                ),
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

    async def record_external_hold_result(
        self,
        *,
        reservation_id: UUID,
        stock_source_id: UUID,
        status: ReservationLineStatus,
        external_hold_ref: str | None,
    ) -> None:
        values: dict[str, object] = {"status": status}
        if external_hold_ref is not None:
            values["external_hold_ref"] = external_hold_ref
        if status == ReservationLineStatus.HELD:
            values["held_at"] = datetime.now(timezone.utc)

        await self._session.execute(
            update(ReservationLineModel)
            .where(
                ReservationLineModel.reservation_id == reservation_id,
                ReservationLineModel.stock_source_id == stock_source_id,
                ReservationLineModel.status == ReservationLineStatus.HOLD_PENDING,
            )
            .values(**values)
        )
        await self._session.flush()

    async def activate_if_all_lines_held(self, reservation_id: UUID) -> bool:
        has_non_held_line = exists(
            select(1).where(
                ReservationLineModel.reservation_id == reservation_id,
                ReservationLineModel.status != ReservationLineStatus.HELD,
            )
        )
        result = await self._session.execute(
            update(ReservationModel)
            .where(
                ReservationModel.id == reservation_id,
                ReservationModel.status == ReservationStatus.RESERVING,
                ReservationModel.expires_at > func.now(),
                ~has_non_held_line,
            )
            .values(status=ReservationStatus.ACTIVE)
        )
        await self._session.flush()
        return result.rowcount == 1

    async def claim_line_for_release(
        self, reservation_id: UUID, stock_source_id: UUID
    ) -> bool:
        result = await self._session.execute(
            update(ReservationLineModel)
            .where(
                ReservationLineModel.reservation_id == reservation_id,
                ReservationLineModel.stock_source_id == stock_source_id,
                ReservationLineModel.status == ReservationLineStatus.HELD,
            )
            .values(status=ReservationLineStatus.RELEASE_PENDING)
        )
        await self._session.flush()
        return result.rowcount == 1

    async def mark_line_released(
        self, reservation_id: UUID, stock_source_id: UUID
    ) -> None:
        await self._session.execute(
            update(ReservationLineModel)
            .where(
                ReservationLineModel.reservation_id == reservation_id,
                ReservationLineModel.stock_source_id == stock_source_id,
                ReservationLineModel.status == ReservationLineStatus.RELEASE_PENDING,
            )
            .values(
                status=ReservationLineStatus.RELEASED,
                released_at=datetime.now(timezone.utc),
            )
        )
        await self._session.flush()

    async def get_pending_external_releases(
        self, reservation_id: UUID
    ) -> tuple[ExternalReleaseRecord, ...]:
        rows = (
            await self._session.execute(
                select(
                    ReservationLineModel.stock_source_id,
                    ReservationLineModel.external_hold_ref,
                ).where(
                    ReservationLineModel.reservation_id == reservation_id,
                    ReservationLineModel.status == ReservationLineStatus.RELEASE_PENDING,
                    ReservationLineModel.external_hold_ref.is_not(None),
                )
            )
        ).all()
        return tuple(
            ExternalReleaseRecord(
                stock_source_id=stock_source_id,
                external_hold_ref=external_hold_ref,
            )
            for stock_source_id, external_hold_ref in rows
        )

    async def record_external_release_result(
        self,
        *,
        reservation_id: UUID,
        stock_source_id: UUID,
        status: ReservationLineStatus,
    ) -> None:
        values: dict[str, object] = {"status": status}
        if status == ReservationLineStatus.RELEASED:
            values["released_at"] = datetime.now(timezone.utc)
        await self._session.execute(
            update(ReservationLineModel)
            .where(
                ReservationLineModel.reservation_id == reservation_id,
                ReservationLineModel.stock_source_id == stock_source_id,
                ReservationLineModel.status == ReservationLineStatus.RELEASE_PENDING,
            )
            .values(**values)
        )
        await self._session.flush()

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
                status=ReservationStatus.CANCELLED,
                release_reason="CREATE_FAILED",
            )
        )
        await self._session.flush()
        return result.rowcount == 1

    async def get_next_pending_external_hold(
        self,
    ) -> PendingExternalHoldRecord | None:
        row = (
            await self._session.execute(
                select(
                    ReservationLineModel.reservation_id,
                    ReservationLineModel.stock_source_id,
                    StockSourceModel.provider_id,
                    ReservationLineModel.quantity,
                    ReservationModel.expires_at,
                )
                .join(
                    ReservationModel,
                    ReservationModel.id == ReservationLineModel.reservation_id,
                )
                .join(
                    StockSourceModel,
                    StockSourceModel.id == ReservationLineModel.stock_source_id,
                )
                .join(
                    InventoryProviderModel,
                    InventoryProviderModel.id == StockSourceModel.provider_id,
                )
                .where(
                    ReservationLineModel.status == ReservationLineStatus.HOLD_PENDING,
                    ReservationModel.status == ReservationStatus.RESERVING,
                    InventoryProviderModel.kind == ProviderKind.EXTERNAL,
                )
                .order_by(ReservationModel.created_at, ReservationLineModel.id)
                .limit(1)
            )
        ).one_or_none()
        if row is None:
            return None
        return PendingExternalHoldRecord(
            reservation_id=row.reservation_id,
            stock_source_id=row.stock_source_id,
            provider_id=row.provider_id,
            quantity=row.quantity,
            expires_at=_as_utc(row.expires_at),
        )

    async def is_external_hold_pending(
        self, reservation_id: UUID, stock_source_id: UUID
    ) -> bool:
        return (
            await self._session.scalar(
                select(ReservationLineModel.id).where(
                    ReservationLineModel.reservation_id == reservation_id,
                    ReservationLineModel.stock_source_id == stock_source_id,
                    ReservationLineModel.status == ReservationLineStatus.HOLD_PENDING,
                )
            )
            is not None
        )

    async def begin_releasing_if_reserving(self, reservation_id: UUID) -> bool:
        result = await self._session.execute(
            update(ReservationModel)
            .where(
                ReservationModel.id == reservation_id,
                ReservationModel.status == ReservationStatus.RESERVING,
            )
            .values(status=ReservationStatus.RELEASING)
        )
        await self._session.flush()
        return result.rowcount == 1

    async def is_external_release_pending(
        self, reservation_id: UUID, stock_source_id: UUID
    ) -> bool:
        return (
            await self._session.scalar(
                select(ReservationLineModel.id).where(
                    ReservationLineModel.reservation_id == reservation_id,
                    ReservationLineModel.stock_source_id == stock_source_id,
                    ReservationLineModel.status == ReservationLineStatus.RELEASE_PENDING,
                )
            )
            is not None
        )

    async def is_releasing(self, reservation_id: UUID) -> bool:
        return (
            await self._session.scalar(
                select(ReservationModel.id).where(
                    ReservationModel.id == reservation_id,
                    ReservationModel.status == ReservationStatus.RELEASING,
                )
            )
            is not None
        )

    async def get_next_unknown_external_hold(
        self,
    ) -> PendingExternalHoldRecord | None:
        row = await self._next_external_line_by_status(ReservationLineStatus.HOLD_UNKNOWN)
        if row is None:
            return None
        return PendingExternalHoldRecord(
            reservation_id=row.reservation_id,
            stock_source_id=row.stock_source_id,
            provider_id=row.provider_id,
            quantity=row.quantity,
            expires_at=_as_utc(row.expires_at),
        )

    async def get_next_unknown_external_release(
        self,
    ) -> PendingExternalReleaseRecord | None:
        row = await self._next_external_line_by_status(
            ReservationLineStatus.RELEASE_UNKNOWN,
            require_hold_ref=True,
        )
        if row is None:
            return None
        return PendingExternalReleaseRecord(
            reservation_id=row.reservation_id,
            stock_source_id=row.stock_source_id,
            provider_id=row.provider_id,
            external_hold_ref=row.external_hold_ref,
        )

    async def reconcile_external_hold_result(
        self,
        *,
        reservation_id: UUID,
        stock_source_id: UUID,
        status: ReservationLineStatus,
        external_hold_ref: str | None,
    ) -> None:
        values: dict[str, object] = {"status": status}
        if external_hold_ref is not None:
            values["external_hold_ref"] = external_hold_ref
        if status == ReservationLineStatus.HELD:
            values["held_at"] = datetime.now(timezone.utc)
        await self._session.execute(
            update(ReservationLineModel)
            .where(
                ReservationLineModel.reservation_id == reservation_id,
                ReservationLineModel.stock_source_id == stock_source_id,
                ReservationLineModel.status == ReservationLineStatus.HOLD_UNKNOWN,
            )
            .values(**values)
        )
        await self._session.flush()

    async def mark_unknown_release_released(
        self, reservation_id: UUID, stock_source_id: UUID
    ) -> None:
        await self._session.execute(
            update(ReservationLineModel)
            .where(
                ReservationLineModel.reservation_id == reservation_id,
                ReservationLineModel.stock_source_id == stock_source_id,
                ReservationLineModel.status == ReservationLineStatus.RELEASE_UNKNOWN,
            )
            .values(
                status=ReservationLineStatus.RELEASED,
                released_at=datetime.now(timezone.utc),
            )
        )
        await self._session.flush()

    async def restore_release_pending_from_unknown(
        self, reservation_id: UUID, stock_source_id: UUID
    ) -> None:
        await self._session.execute(
            update(ReservationLineModel)
            .where(
                ReservationLineModel.reservation_id == reservation_id,
                ReservationLineModel.stock_source_id == stock_source_id,
                ReservationLineModel.status == ReservationLineStatus.RELEASE_UNKNOWN,
            )
            .values(status=ReservationLineStatus.RELEASE_PENDING)
        )
        await self._session.flush()

    async def claim_next_expired_reserving_reservation(self) -> UUID | None:
        reservation_id = await self._session.scalar(
            select(ReservationModel.id)
            .where(
                ReservationModel.status == ReservationStatus.RESERVING,
                ReservationModel.expires_at <= func.now(),
            )
            .order_by(ReservationModel.expires_at, ReservationModel.id)
            .limit(1)
        )
        if reservation_id is None:
            return None
        return await self._session.scalar(
            update(ReservationModel)
            .where(
                ReservationModel.id == reservation_id,
                ReservationModel.status == ReservationStatus.RESERVING,
                ReservationModel.expires_at <= func.now(),
            )
            .values(status=ReservationStatus.RELEASING, release_reason="EXPIRED")
            .returning(ReservationModel.id)
        )

    async def get_next_releasing_reservation_id(self) -> UUID | None:
        return await self._session.scalar(
            select(ReservationModel.id)
            .where(ReservationModel.status == ReservationStatus.RELEASING)
            .order_by(ReservationModel.updated_at, ReservationModel.id)
            .limit(1)
        )

    async def _next_external_line_by_status(
        self,
        status: ReservationLineStatus,
        *,
        require_hold_ref: bool = False,
    ):
        conditions = [
            ReservationLineModel.status == status,
            InventoryProviderModel.kind == ProviderKind.EXTERNAL,
        ]
        if require_hold_ref:
            conditions.append(ReservationLineModel.external_hold_ref.is_not(None))
        return (
            await self._session.execute(
                select(
                    ReservationLineModel.reservation_id,
                    ReservationLineModel.stock_source_id,
                    StockSourceModel.provider_id,
                    ReservationLineModel.quantity,
                    ReservationModel.expires_at,
                    ReservationLineModel.external_hold_ref,
                )
                .join(
                    ReservationModel,
                    ReservationModel.id == ReservationLineModel.reservation_id,
                )
                .join(
                    StockSourceModel,
                    StockSourceModel.id == ReservationLineModel.stock_source_id,
                )
                .join(
                    InventoryProviderModel,
                    InventoryProviderModel.id == StockSourceModel.provider_id,
                )
                .where(*conditions)
                .order_by(ReservationModel.created_at, ReservationLineModel.id)
                .limit(1)
            )
        ).one_or_none()

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
