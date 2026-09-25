from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, status

from app.api.schemas.reservations import (
    ConfirmReservationResponse,
    CreateReservationRequest,
    CreateReservationResponse,
    ReservationResponse,
)
from app.application.dto.reservations import (
    CreateReservationCommand,
    ReservationItemCommand,
)
from app.application.services.cancel_reservation import CancelReservationService
from app.application.services.confirm_reservation import ConfirmReservationService
from app.application.services.create_reservation import CreateReservationService
from app.application.services.get_reservation import GetReservationService
from app.bootstrap.dependencies import (
    get_cancel_reservation_service,
    get_confirm_reservation_service,
    get_create_reservation_service,
    get_reservation_service,
)
from app.domain.enums import ReservationStatus


router = APIRouter(prefix="/reservations", tags=["reservations"])


def _reservation_response(result) -> ReservationResponse:
    return ReservationResponse(
        reservation_id=result.reservation_id,
        status=result.status,
        created_at=result.created_at,
        expires_at=result.expires_at,
        payment_allowed=result.payment_allowed,
        requires_attention=result.requires_attention,
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


@router.post("", response_model=CreateReservationResponse)
async def create_reservation(
    body: CreateReservationRequest,
    response: Response,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
    ),
    user_id: str = Header(..., alias="X-User-Id", min_length=1, max_length=160),
    service: CreateReservationService = Depends(get_create_reservation_service),
) -> CreateReservationResponse:
    result = await service.execute(
        CreateReservationCommand(
            user_id=user_id,
            idempotency_key=idempotency_key,
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
    response.headers["Location"] = f"/reservations/{result.reservation_id}"

    if result.status in {ReservationStatus.RESERVING, ReservationStatus.RELEASING}:
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Retry-After"] = "1"
    elif result.replayed:
        response.status_code = status.HTTP_200_OK
    else:
        response.status_code = status.HTTP_201_CREATED

    return CreateReservationResponse(**_reservation_response(result).model_dump())


@router.get("/{reservation_id}", response_model=ReservationResponse)
async def get_reservation(
    reservation_id: UUID,
    user_id: str = Header(..., alias="X-User-Id", min_length=1, max_length=160),
    service: GetReservationService = Depends(get_reservation_service),
) -> ReservationResponse:
    return _reservation_response(
        await service.execute(reservation_id, user_id=user_id)
    )


@router.post(
    "/{reservation_id}/confirm",
    response_model=ConfirmReservationResponse,
)
async def confirm_reservation(
    reservation_id: UUID,
    user_id: str = Header(..., alias="X-User-Id", min_length=1, max_length=160),
    service: ConfirmReservationService = Depends(get_confirm_reservation_service),
) -> ConfirmReservationResponse:
    result = await service.execute(reservation_id, user_id=user_id)
    base = _reservation_response(result)
    return ConfirmReservationResponse(
        **base.model_dump(),
        order_id=result.order_id,
    )


@router.post(
    "/{reservation_id}/cancel",
    response_model=ReservationResponse,
)
async def cancel_reservation(
    reservation_id: UUID,
    response: Response,
    user_id: str = Header(..., alias="X-User-Id", min_length=1, max_length=160),
    service: CancelReservationService = Depends(get_cancel_reservation_service),
) -> ReservationResponse:
    result = await service.execute(reservation_id, user_id=user_id)
    if result.status == ReservationStatus.RELEASING:
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Retry-After"] = "1"
    else:
        response.status_code = status.HTTP_200_OK
    return _reservation_response(result)
