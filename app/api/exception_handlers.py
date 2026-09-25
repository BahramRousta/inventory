from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.application.errors import (
    ApplicationError,
    IdempotencyConflict,
    InsufficientStock,
    InvalidReservationItems,
    ProductSourceMismatch,
    ReservationExpired,
    ReservationNotFound,
    ReservationStateConflict,
    SourceDisabled,
    SourceNotReservable,
)


ERROR_STATUS = {
    InvalidReservationItems: 422,
    ProductSourceMismatch: 422,
    SourceDisabled: 422,
    SourceNotReservable: 422,
    InsufficientStock: 409,
    IdempotencyConflict: 409,
    ReservationNotFound: 404,
    ReservationStateConflict: 409,
    ReservationExpired: 409,
}


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApplicationError)
    async def handle_application_error(_: Request, exc: ApplicationError) -> JSONResponse:
        status_code = 500
        for error_type, mapped_status in ERROR_STATUS.items():
            if isinstance(exc, error_type):
                status_code = mapped_status
                break
        return JSONResponse(
            status_code=status_code,
            content={"code": exc.code, "message": exc.message},
        )
