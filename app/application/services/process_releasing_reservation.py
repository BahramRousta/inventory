from typing import Callable
from uuid import UUID

from app.application.dto.reservations import ExternalReleaseRecord
from app.application.ports.provider_gateway import (
    ProviderGateway,
    ProviderGatewayRegistry,
    ProviderReleaseOutcome,
    ProviderReleaseResult,
)
from app.application.ports.provider_work_lock import ProviderWorkLock
from app.application.ports.repositories import UnitOfWork
from app.domain.enums import ProviderKind, ReservationLineStatus, ReservationStatus


class ProcessReleasingReservationService:
    """Executes compensation for one reservation already in RELEASING."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        provider_gateways: ProviderGatewayRegistry,
        work_lock: ProviderWorkLock,
    ) -> None:
        self._uow_factory = uow_factory
        self._provider_gateways = provider_gateways
        self._work_lock = work_lock

    async def execute(self, reservation_id: UUID) -> bool:
        async with self._uow_factory() as uow:
            if not await uow.reservations.is_releasing(reservation_id):
                return False
            lines = await uow.reservations.get_lines(reservation_id)
            if not lines:
                return False
            source_ids = tuple(line.stock_source_id for line in lines)
            sources = await uow.stock_sources.get_many(source_ids)

            for line in lines:
                if line.status != ReservationLineStatus.HELD:
                    continue
                if not await uow.reservations.claim_line_for_release(
                    reservation_id, line.stock_source_id
                ):
                    continue

                source = sources[line.stock_source_id]
                if source.provider_kind == ProviderKind.EXTERNAL:
                    continue

                if not await uow.inventory.release_hold(
                    line.stock_source_id, line.quantity
                ):
                    raise RuntimeError(
                        f"Could not release internal hold for source {line.stock_source_id}."
                    )
                await uow.reservations.mark_line_released(
                    reservation_id, line.stock_source_id
                )
            await uow.commit()

        async with self._uow_factory() as uow:
            pending_releases = await uow.reservations.get_pending_external_releases(
                reservation_id
            )

        for pending_release in pending_releases:
            source = sources[pending_release.stock_source_id]
            await self._process_external_release(
                reservation_id=reservation_id,
                provider_id=source.provider_id,
                pending_release=pending_release,
            )

        async with self._uow_factory() as uow:
            await uow.reservations.cancel_if_all_lines_resolved(reservation_id)
            await uow.commit()
        return True

    async def _process_external_release(
        self,
        *,
        reservation_id: UUID,
        provider_id: UUID,
        pending_release: ExternalReleaseRecord,
    ) -> None:
        release_key = f"{reservation_id}:{pending_release.stock_source_id}:RELEASE"
        async with self._work_lock.try_acquire(release_key) as acquired:
            if not acquired:
                return

            async with self._uow_factory() as uow:
                if not await uow.reservations.is_external_release_pending(
                    reservation_id, pending_release.stock_source_id
                ):
                    return

            gateway = self._provider_gateways.get(provider_id)
            result = await self._attempt_release(
                gateway=gateway,
                stock_source_id=pending_release.stock_source_id,
                external_hold_ref=pending_release.external_hold_ref,
                release_key=release_key,
            )
            line_status = (
                ReservationLineStatus.RELEASED
                if result.outcome == ProviderReleaseOutcome.RELEASED
                else ReservationLineStatus.RELEASE_UNKNOWN
            )
            async with self._uow_factory() as uow:
                await uow.reservations.record_external_release_result(
                    reservation_id=reservation_id,
                    stock_source_id=pending_release.stock_source_id,
                    status=line_status,
                )
                await uow.commit()

    @staticmethod
    async def _attempt_release(
        *,
        gateway: ProviderGateway | None,
        stock_source_id: UUID,
        external_hold_ref: str,
        release_key: str,
    ) -> ProviderReleaseResult:
        if gateway is None:
            return ProviderReleaseResult(
                outcome=ProviderReleaseOutcome.UNKNOWN,
                error_code="PROVIDER_GATEWAY_UNAVAILABLE",
            )
        try:
            return await gateway.release(
                stock_source_id=stock_source_id,
                external_hold_ref=external_hold_ref,
                release_key=release_key,
            )
        except Exception:
            return ProviderReleaseResult(
                outcome=ProviderReleaseOutcome.UNKNOWN,
                error_code="PROVIDER_GATEWAY_EXCEPTION",
            )
