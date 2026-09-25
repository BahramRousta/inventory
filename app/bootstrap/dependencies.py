from app.application.ports.provider_gateway import InMemoryProviderGatewayRegistry
from app.application.services.cancel_reservation import CancelReservationService
from app.application.services.confirm_reservation import ConfirmReservationService
from app.application.services.create_reservation import CreateReservationService
from app.application.services.get_reservation import GetReservationService
from app.application.services.process_pending_provider_hold import (
    ProcessPendingProviderHoldService,
)
from app.application.services.process_releasing_reservation import (
    ProcessReleasingReservationService,
)
from app.application.services.provider_worker_tick import ProviderWorkerTickService
from app.application.services.reconcile_provider_work import ReconcileProviderWorkService
from app.application.services.select_pending_provider_hold import (
    SelectPendingProviderHoldService,
)
from app.bootstrap.config import get_settings
from app.infrastructure.clock import SystemClock
from app.infrastructure.db.provider_work_lock import PostgresProviderWorkLock
from app.infrastructure.db.session import AsyncSessionLocal, engine
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.infrastructure.providers.external_hold_http import ExternalHoldHttpGateway


def _uow_factory():
    return SqlAlchemyUnitOfWork(AsyncSessionLocal)


def get_create_reservation_service() -> CreateReservationService:
    settings = get_settings()
    gateways = {}
    if (
        settings.external_provider_id is not None
        and settings.external_provider_base_url is not None
    ):
        gateways[settings.external_provider_id] = ExternalHoldHttpGateway(
            base_url=settings.external_provider_base_url,
            hold_timeout_seconds=settings.external_provider_hold_timeout_seconds,
        )
    return CreateReservationService(
        uow_factory=_uow_factory,
        clock=SystemClock(),
        ttl_seconds=settings.reservation_ttl_seconds,
        provider_gateways=InMemoryProviderGatewayRegistry(gateways),
    )


def get_reservation_service() -> GetReservationService:
    return GetReservationService(uow_factory=_uow_factory)


def get_confirm_reservation_service() -> ConfirmReservationService:
    return ConfirmReservationService(uow_factory=_uow_factory)


def get_cancel_reservation_service() -> CancelReservationService:
    return CancelReservationService(uow_factory=_uow_factory)


def get_provider_worker_tick_service() -> ProviderWorkerTickService:
    settings = get_settings()
    gateways = {}
    if (
        settings.external_provider_id is not None
        and settings.external_provider_base_url is not None
    ):
        gateways[settings.external_provider_id] = ExternalHoldHttpGateway(
            base_url=settings.external_provider_base_url,
            hold_timeout_seconds=settings.external_provider_hold_timeout_seconds,
        )
    registry = InMemoryProviderGatewayRegistry(gateways)
    work_lock = PostgresProviderWorkLock(engine)
    return ProviderWorkerTickService(
        uow_factory=_uow_factory,
        select_pending_hold=SelectPendingProviderHoldService(uow_factory=_uow_factory),
        process_pending_hold=ProcessPendingProviderHoldService(
            uow_factory=_uow_factory,
            provider_gateways=registry,
            work_lock=work_lock,
        ),
        process_releasing=ProcessReleasingReservationService(
            uow_factory=_uow_factory,
            provider_gateways=registry,
            work_lock=work_lock,
        ),
        reconcile_work=ReconcileProviderWorkService(
            uow_factory=_uow_factory,
            provider_gateways=registry,
            work_lock=work_lock,
        ),
    )
