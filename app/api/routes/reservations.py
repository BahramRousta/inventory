from fastapi import APIRouter, Depends, Header, Response, status

from app.api.schemas.reservations import CreateReservationRequest, CreateReservationResponse
from app.application.dto.reservations import CreateReservationCommand, ReservationItemCommand
from app.application.services.create_reservation import CreateReservationService
from app.bootstrap.dependencies import get_create_reservation_service


router = APIRouter(prefix="/reservations", tags=["reservations"])


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
    # Internal-only slice always resolves synchronously to ACTIVE.
    response.status_code = status.HTTP_201_CREATED
    return CreateReservationResponse(
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
