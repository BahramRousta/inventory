import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.bootstrap.config import get_settings


app = FastAPI(title="Fake Inventory Provider", version="0.1.0")


class HoldRequest(BaseModel):
    stock_source_id: str
    quantity: int = Field(gt=0)
    operation_key: str
    expires_at: datetime


@dataclass
class HoldState:
    operation_key: str
    hold_ref: str
    expires_at: datetime
    released: bool = False


_holds_by_key: dict[str, HoldState] = {}
_holds_by_ref: dict[str, HoldState] = {}
_lock = asyncio.Lock()
_mode = get_settings().fake_provider_mode


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": _mode}


@app.post("/admin/mode/{mode}")
async def set_mode(mode: str) -> dict[str, str]:
    global _mode
    if mode not in {"success", "decline", "timeout_after_side_effect"}:
        raise HTTPException(status_code=422, detail="unsupported fake-provider mode")
    _mode = mode
    return {"mode": _mode}


@app.post("/holds")
async def create_hold(
    body: HoldRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    if idempotency_key != body.operation_key:
        raise HTTPException(status_code=422, detail="idempotency key mismatch")

    async with _lock:
        existing = _holds_by_key.get(idempotency_key)
        if existing is not None:
            if existing.released:
                raise HTTPException(status_code=409, detail="hold already released")
            return {
                "hold_ref": existing.hold_ref,
                "expires_at": existing.expires_at.isoformat(),
            }

        if _mode == "decline":
            raise HTTPException(status_code=409, detail="fake provider declined hold")

        hold_ref = str(uuid5(NAMESPACE_URL, idempotency_key))
        provider_expiry = max(
            body.expires_at + timedelta(minutes=5),
            datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        hold = HoldState(
            operation_key=idempotency_key,
            hold_ref=hold_ref,
            expires_at=provider_expiry,
        )
        _holds_by_key[idempotency_key] = hold
        _holds_by_ref[hold_ref] = hold

    if _mode == "timeout_after_side_effect":
        await asyncio.sleep(get_settings().fake_provider_timeout_seconds)

    return {
        "hold_ref": hold.hold_ref,
        "expires_at": hold.expires_at.isoformat(),
    }


@app.get("/holds/{hold_key}")
async def get_hold(hold_key: str):
    async with _lock:
        hold = _holds_by_key.get(hold_key)
        if hold is None:
            raise HTTPException(status_code=404, detail="hold not found")
        if hold.released:
            return {
                "status": "RELEASED",
                "hold_ref": hold.hold_ref,
            }
        return {
            "status": "HELD",
            "hold_ref": hold.hold_ref,
            "expires_at": hold.expires_at.isoformat(),
        }


@app.post("/holds/{hold_ref}/release")
async def release_hold(
    hold_ref: str,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    del idempotency_key
    async with _lock:
        hold = _holds_by_ref.get(hold_ref)
        if hold is None:
            raise HTTPException(status_code=404, detail="hold not found")
        hold.released = True
    return {"status": "RELEASED"}
