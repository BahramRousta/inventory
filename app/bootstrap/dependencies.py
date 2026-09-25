from app.application.ports.provider_gateway import InMemoryProviderGatewayRegistry
from app.application.services.create_reservation import CreateReservationService
from app.application.services.process_pending_provider_hold import (
    ProcessPendingProviderHoldService,
)
from app.application.services.process_releasing_reservation import (
    ProcessReleasingReservationService,
)
from app.application.services.reconcile_provider_work import ReconcileProviderWorkService
from app.bootstrap.config import get_settings
from app.infrastructure.clock import SystemClock
from app.infrastructure.db.provider_work_lock import PostgresProviderWorkLock
from app.infrastructure.db.session import AsyncSessionLocal, engine
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.infrastructure.providers.external_hold_http import ExternalHoldHttpGateway


def get_create_reservation_service() -> CreateReservationService:
    settings = get_settings()
    return CreateReservationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(AsyncSessionLocal),
        clock=SystemClock(),
        ttl_seconds=settings.reservation_ttl_seconds,
        provider_gateways=get_provider_gateway_registry(),
    )


def get_provider_gateway_registry() -> InMemoryProviderGatewayRegistry:
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
    return InMemoryProviderGatewayRegistry(gateways)


def get_process_pending_provider_hold_service() -> ProcessPendingProviderHoldService:
    return ProcessPendingProviderHoldService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(AsyncSessionLocal),
        provider_gateways=get_provider_gateway_registry(),
        work_lock=PostgresProviderWorkLock(engine),
    )


def get_process_releasing_reservation_service() -> ProcessReleasingReservationService:
    return ProcessReleasingReservationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(AsyncSessionLocal),
        provider_gateways=get_provider_gateway_registry(),
        work_lock=PostgresProviderWorkLock(engine),
    )


def get_reconcile_provider_work_service() -> ReconcileProviderWorkService:
    return ReconcileProviderWorkService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(AsyncSessionLocal),
        provider_gateways=get_provider_gateway_registry(),
        work_lock=PostgresProviderWorkLock(engine),
    )
