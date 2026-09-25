from typing import Callable

from app.application.dto.reservations import (
    ClaimedExternalHoldRecord,
    ClaimedExternalReleaseRecord,
)
from app.application.ports.provider_gateway import (
    ProviderRegistryProtocol,
    ProviderReservationLookupOutcome,
    ProviderReservationLookupResult,
)
from app.application.ports.repositories import UnitOfWork
from app.domain.enums import ReservationLineStatus


class ReconcileProviderWorkService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        providers: ProviderRegistryProtocol,
    ) -> None:
        self._uow_factory = uow_factory
        self._providers = providers

    async def reconcile_hold(
        self,
        work: ClaimedExternalHoldRecord,
    ) -> bool:
        result = await self._lookup(
            work.provider_id,
            f"{work.reservation_id}:{work.stock_source_id}:RESERVE",
        )

        async with self._uow_factory() as uow:
            if result.outcome == ProviderReservationLookupOutcome.UNKNOWN:
                persisted = (
                    await uow.reservations.return_hold_claim_to_unknown(
                        work.reservation_id,
                        work.stock_source_id,
                        work.claim_token,
                    )
                )
            else:
                status = (
                    ReservationLineStatus.HELD
                    if result.outcome
                    == ProviderReservationLookupOutcome.RESERVED
                    else ReservationLineStatus.FAILED
                )
                persisted = (
                    await uow.reservations.record_external_hold_result(
                        reservation_id=work.reservation_id,
                        stock_source_id=work.stock_source_id,
                        claim_token=work.claim_token,
                        status=status,
                        external_hold_ref=result.external_ref,
                    )
                )

                if persisted and status == ReservationLineStatus.HELD:
                    if await uow.reservations.is_releasing(
                        work.reservation_id
                    ):
                        await uow.reservations.claim_line_for_release(
                            work.reservation_id,
                            work.stock_source_id,
                        )
                    else:
                        await uow.reservations.activate_if_all_lines_held(
                            work.reservation_id
                        )
                elif persisted:
                    await uow.reservations.begin_releasing_if_reserving(
                        work.reservation_id
                    )

            if persisted:
                await uow.commit()
            return persisted

    async def reconcile_release(
        self,
        work: ClaimedExternalReleaseRecord,
    ) -> bool:
        result = await self._lookup(
            work.provider_id,
            f"{work.reservation_id}:{work.stock_source_id}:RESERVE",
        )

        async with self._uow_factory() as uow:
            if result.outcome == ProviderReservationLookupOutcome.UNKNOWN:
                persisted = (
                    await uow.reservations.return_release_claim_to_unknown(
                        work.reservation_id,
                        work.stock_source_id,
                        work.claim_token,
                    )
                )
            elif (
                result.outcome
                == ProviderReservationLookupOutcome.NOT_RESERVED
            ):
                persisted = (
                    await uow.reservations.record_external_release_result(
                        reservation_id=work.reservation_id,
                        stock_source_id=work.stock_source_id,
                        claim_token=work.claim_token,
                        status=ReservationLineStatus.RELEASED,
                    )
                )
                if persisted:
                    await uow.reservations.cancel_if_all_lines_resolved(
                        work.reservation_id
                    )
            else:
                persisted = (
                    await uow.reservations.return_release_claim_to_pending(
                        work.reservation_id,
                        work.stock_source_id,
                        work.claim_token,
                    )
                )

            if persisted:
                await uow.commit()
            return persisted

    async def _lookup(
        self,
        provider_id,
        reservation_key: str,
    ) -> ProviderReservationLookupResult:
        provider = self._providers.get(provider_id)
        if provider is None:
            return ProviderReservationLookupResult(
                outcome=ProviderReservationLookupOutcome.UNKNOWN,
                error_code="PROVIDER_NOT_CONFIGURED",
            )

        try:
            return await provider.get_reservation(
                reservation_key=reservation_key
            )
        except Exception:
            return ProviderReservationLookupResult(
                outcome=ProviderReservationLookupOutcome.UNKNOWN,
                error_code="PROVIDER_LOOKUP_EXCEPTION",
            )
