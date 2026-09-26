from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.application.errors import (
    ApplicationError,
    InsufficientStock,
    ProductSourceMismatch,
    ReservationExpired,
    ReservationNotFound,
    ReservationStateConflict,
    SourceDisabled,
)


ERROR_STATUS = {
    ProductSourceMismatch: 422,
    SourceDisabled: 422,
    InsufficientStock: 409,
    ReservationNotFound: 404,
    ReservationExpired: 409,
    ReservationStateConflict: 409,
}


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApplicationError)
    async def handle_application_error(_: Request, exc: ApplicationError) -> JSONResponse:
        status_code = next(
            (mapped for error_type, mapped in ERROR_STATUS.items() if isinstance(exc, error_type)),
            500,
        )
        return JSONResponse(
            status_code=status_code,
            content={"code": exc.code, "message": exc.message},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else None
        message = (
            first.get("msg", "Request validation failed.")
            if first
            else "Request validation failed."
        )
        return JSONResponse(
            status_code=422,
            content={"code": "VALIDATION_ERROR", "message": message},
        )
