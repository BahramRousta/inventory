from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, status

from app.api.schemas.reservations import CreateReservationRequest, CreateReservationResponse, ReservationResponse, \
    ConfirmReservationResponse
from app.application.dto.reservations import CreateReservationCommand, ReservationItemCommand
from app.application.services.cancel_reservation import CancelReservationService
from app.application.services.confirm_reservation import ConfirmReservationService
from app.application.services.create_reservation import CreateReservationService
from app.application.services.get_reservation import GetReservationService
from app.bootstrap.dependencies import get_create_reservation_service, get_reservation_service, \
    get_confirm_reservation_service, get_cancel_reservation_service
from app.domain.enums import ReservationStatus


router = APIRouter(prefix="/reservations", tags=["reservations"])


def _reservation_response(result) -> ReservationResponse:
    return ReservationResponse(
        reservation_id=result.reservation_id,
        status=result.status,
        expires_at=result.expires_at,
        payment_allowed=result.payment_allowed,
        lines=[
            {
                "product_id": line.product_id,
                "stock_source_id": line.stock_source_id,
                "quantity": line.quantity,
                "status": line.status,
            }
            for line in result.lines
        ],
    )

@router.post("", response_model=CreateReservationResponse, status_code=status.HTTP_201_CREATED)
async def create_reservation(
    body: CreateReservationRequest,
    response: Response,
    service: CreateReservationService = Depends(get_create_reservation_service),
) -> CreateReservationResponse:
    result = await service.execute(
        CreateReservationCommand(
            user_id=body.user_id,
            idempotency_key=body.idempotency_key,
            items=tuple(
                ReservationItemCommand(
                    product_id=item.product_id,
                    stock_source_id=item.stock_source_id,
                    quantity=item.quantity,
                )
                for item in body.items
            ),
        )
    )
    response.status_code = (
        status.HTTP_202_ACCEPTED
        if result.status in {ReservationStatus.RESERVING, ReservationStatus.RELEASING}
        else status.HTTP_201_CREATED
    )
    return CreateReservationResponse(**_reservation_response(result).model_dump())


@router.get("/{reservation_id}", response_model=ReservationResponse)
async def get_reservation(
    reservation_id: UUID,
    service: GetReservationService = Depends(get_reservation_service),
) -> ReservationResponse:
    return _reservation_response(await service.execute(reservation_id))


@router.post("/{reservation_id}/confirm", response_model=ConfirmReservationResponse)
async def confirm_reservation(
    reservation_id: UUID,
    service: ConfirmReservationService = Depends(get_confirm_reservation_service),
) -> ConfirmReservationResponse:
    result = await service.execute(reservation_id)
    base = _reservation_response(result)
    return ConfirmReservationResponse(
        **base.model_dump(),
        order_id=result.order_id,
    )


@router.post(
    "/{reservation_id}/cancel",
    response_model=ReservationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_reservation(
    reservation_id: UUID,
    service: CancelReservationService = Depends(get_cancel_reservation_service),
) -> ReservationResponse:
    return _reservation_response(await service.execute(reservation_id))