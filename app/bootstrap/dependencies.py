from app.application.ports.provider_gateway import ProviderRegistryProtocol
from app.application.services.cancel.cancel_reservation import CancelReservationService
from app.application.services.confirm.confirm_reservation import ConfirmReservationService
from app.application.services.create.create_reservation import CreateReservationService
from app.application.services.inquiry.get_reservation import GetReservationService
from app.application.services.cancel.process_claimed_provider_release import (
    ProcessClaimedProviderReleaseService,
)
from app.application.services.create.process_pending_provider_hold import (
    ProcessPendingProviderHoldService,
)
from app.application.services.cancel.process_releasing_reservation import (
    ProcessReleasingReservationService,
)
from app.application.services.reconciliation.reconcile_provider_work import ReconcileProviderWorkService
from app.bootstrap.config import get_settings
from app.infrastructure.clock import SystemClock
from app.infrastructure.db.session import AsyncSessionLocal
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.infrastructure.providers.factory import ProviderFactory


def _uow_factory():
    return SqlAlchemyUnitOfWork(AsyncSessionLocal)


def get_provider_registry() -> ProviderRegistryProtocol:
    return ProviderFactory(get_settings()).create_registry()


def get_create_reservation_service() -> CreateReservationService:
    settings = get_settings()
    return CreateReservationService(
        uow_factory=_uow_factory,
        clock=SystemClock(),
        ttl_seconds=settings.reservation_ttl_seconds,
    )


def get_process_pending_provider_hold_service() -> ProcessPendingProviderHoldService:
    return ProcessPendingProviderHoldService(
        uow_factory=_uow_factory,
        providers=get_provider_registry(),
    )


def get_process_releasing_reservation_service() -> ProcessReleasingReservationService:
    return ProcessReleasingReservationService(uow_factory=_uow_factory)


def get_process_claimed_provider_release_service() -> ProcessClaimedProviderReleaseService:
    return ProcessClaimedProviderReleaseService(
        uow_factory=_uow_factory,
        providers=get_provider_registry(),
    )


def get_reconcile_provider_work_service() -> ReconcileProviderWorkService:
    return ReconcileProviderWorkService(
        uow_factory=_uow_factory,
        providers=get_provider_registry(),
    )


def get_reservation_service() -> GetReservationService:
    return GetReservationService(uow_factory=_uow_factory)


def get_confirm_reservation_service() -> ConfirmReservationService:
    return ConfirmReservationService(uow_factory=_uow_factory)


def get_cancel_reservation_service() -> CancelReservationService:
    return CancelReservationService(uow_factory=_uow_factory)
