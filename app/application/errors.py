class ApplicationError(Exception):
    code = "APPLICATION_ERROR"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class InvalidReservationItems(ApplicationError):
    code = "INVALID_RESERVATION_ITEMS"


class ProductSourceMismatch(ApplicationError):
    code = "PRODUCT_SOURCE_MISMATCH"


class SourceDisabled(ApplicationError):
    code = "SOURCE_DISABLED"


class SourceNotReservable(ApplicationError):
    code = "SOURCE_NOT_RESERVABLE"


class InsufficientStock(ApplicationError):
    code = "INSUFFICIENT_STOCK"


class IdempotencyConflict(ApplicationError):
    code = "IDEMPOTENCY_CONFLICT"


class ReservationNotFound(ApplicationError):
    code = "RESERVATION_NOT_FOUND"


class ReservationStateConflict(ApplicationError):
    code = "RESERVATION_STATE_CONFLICT"


class ReservationExpired(ApplicationError):
    code = "RESERVATION_EXPIRED"


class PersistenceConflict(Exception):
    """Infrastructure translated a storage conflict that the application can recover from."""
