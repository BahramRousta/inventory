from fastapi import FastAPI

from app.api.exception_handlers import install_exception_handlers
from app.api.routes.reservations import router as reservations_router


app = FastAPI(title="Inventory Reservation Service", version="0.1.0")
install_exception_handlers(app)
app.include_router(reservations_router)


@app.get("/health", status_code=200)
def health() -> dict[str, str]:
    return {"status": "ok"}
