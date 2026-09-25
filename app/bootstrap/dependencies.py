from app.application.ports.provider_gateway import InMemoryProviderGatewayRegistry
from app.application.services.cancel_reservation import CancelReservationService
from app.application.services.confirm_reservation import ConfirmReservationService
from app.application.services.create_reservation import CreateReservationService
from app.application.services.get_reservation import GetReservationService
from app.application.services.process_claimed_provider_release import (
    ProcessClaimedProviderReleaseService,
)
from app.application.services.process_payment_outcome import ProcessPaymentOutcomeService
from app.application.services.process_pending_provider_hold import (
    ProcessPendingProviderHoldService,
)
from app.application.services.process_releasing_reservation import (
    ProcessReleasingReservationService,
)
from app.application.services.reconcile_provider_work import ReconcileProviderWorkService
from app.bootstrap.config import get_settings
from app.infrastructure.clock import SystemClock
from app.infrastructure.db.session import AsyncSessionLocal
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.infrastructure.providers.factory import ProviderGatewayFactory


def _uow_factory():
    return SqlAlchemyUnitOfWork(AsyncSessionLocal)


def get_provider_gateway_registry() -> InMemoryProviderGatewayRegistry:
    return ProviderGatewayFactory(get_settings()).create_registry()


def get_create_reservation_service() -> CreateReservationService:
    settings = get_settings()
    return CreateReservationService(
        uow_factory=_uow_factory,
        clock=SystemClock(),
        ttl_seconds=settings.reservation_ttl_seconds,
        provider_gateways=get_provider_gateway_registry(),
    )


def get_process_pending_provider_hold_service() -> ProcessPendingProviderHoldService:
    return ProcessPendingProviderHoldService(
        uow_factory=_uow_factory,
        provider_gateways=get_provider_gateway_registry(),
    )


def get_process_releasing_reservation_service() -> ProcessReleasingReservationService:
    return ProcessReleasingReservationService(uow_factory=_uow_factory)


def get_process_claimed_provider_release_service() -> ProcessClaimedProviderReleaseService:
    return ProcessClaimedProviderReleaseService(
        uow_factory=_uow_factory,
        provider_gateways=get_provider_gateway_registry(),
    )


def get_reconcile_provider_work_service() -> ReconcileProviderWorkService:
    return ReconcileProviderWorkService(
        uow_factory=_uow_factory,
        provider_gateways=get_provider_gateway_registry(),
    )


def get_reservation_service() -> GetReservationService:
    return GetReservationService(uow_factory=_uow_factory)


def get_confirm_reservation_service() -> ConfirmReservationService:
    return ConfirmReservationService(
        uow_factory=_uow_factory,
        provider_gateways=get_provider_gateway_registry(),
    )


def get_cancel_reservation_service() -> CancelReservationService:
    return CancelReservationService(uow_factory=_uow_factory)


def get_payment_outcome_service() -> ProcessPaymentOutcomeService:
    return ProcessPaymentOutcomeService(
        uow_factory=_uow_factory,
        provider_gateways=get_provider_gateway_registry(),
    )
