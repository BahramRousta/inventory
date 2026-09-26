class ApplicationError(Exception):
    code = "APPLICATION_ERROR"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ProductSourceMismatch(ApplicationError):
    code = "PRODUCT_SOURCE_MISMATCH"


class SourceDisabled(ApplicationError):
    code = "SOURCE_DISABLED"


class InsufficientStock(ApplicationError):
    code = "INSUFFICIENT_STOCK"


class PersistenceConflict(Exception):
    """Infrastructure translated a storage conflict that the application can recover from."""


class ReservationNotFound(ApplicationError):
    code = "RESERVATION_NOT_FOUND"


class ReservationStateConflict(ApplicationError):
    code = "RESERVATION_STATE_CONFLICT"


class ReservationExpired(ApplicationError):
    code = "RESERVATION_EXPIRED"
