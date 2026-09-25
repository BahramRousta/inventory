from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.enums import ReservationLineStatus, ReservationStatus


class ReservationItemRequest(BaseModel):
    product_id: UUID
    stock_source_id: UUID
    quantity: int = Field(gt=0, le=2_147_483_647)


class CreateReservationRequest(BaseModel):
    items: list[ReservationItemRequest] = Field(min_length=1)



class ReservationLineResponse(BaseModel):
    product_id: UUID
    stock_source_id: UUID
    quantity: int
    status: ReservationLineStatus


class ReservationResponse(BaseModel):
    reservation_id: UUID
    status: ReservationStatus
    created_at: datetime
    expires_at: datetime
    payment_allowed: bool
    requires_attention: bool
    lines: list[ReservationLineResponse]


class CreateReservationResponse(ReservationResponse):
    pass


class ConfirmReservationResponse(ReservationResponse):
    order_id: UUID



class ErrorResponse(BaseModel):
    code: str
    message: str
