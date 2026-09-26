"""api/heartbeat.py — POST /heartbeat (report in), GET /heartbeat (list)."""
from fastapi import APIRouter
from pydantic import BaseModel, Field

from services import heartbeat_service

router = APIRouter()


class HeartbeatIn(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=64, regex=r"^[A-Za-z0-9._:-]+$")


@router.post("/heartbeat")
def post_heartbeat(body: HeartbeatIn) -> dict:
    return heartbeat_service.record(body.device_id)


@router.get("/heartbeat")
def get_heartbeat() -> dict:
    devices = heartbeat_service.list_devices()
    return {"count": len(devices), "devices": devices}
