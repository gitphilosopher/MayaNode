"""api/events.py — POST /events (append), GET /events (list, newest first)."""
from typing import Any, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, validator

from node import clock
from services import event_service

router = APIRouter()


class EventIn(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=64)
    event_type: str = Field(..., min_length=1, max_length=64)
    payload: Any = Field(default_factory=dict)
    occurred_at: Optional[str] = None

    @validator("occurred_at")
    def _validate_occurred_at(cls, v):
        if v is None:
            return v
        try:
            dt = clock.parse_iso(v)
        except ValueError:
            raise ValueError("occurred_at must be a valid ISO-8601 datetime")
        if dt.tzinfo is None:
            raise ValueError("occurred_at must include a UTC offset")
        return clock.to_iso(dt)   # normalize to the same fixed-width form as received_at


@router.post("/events")
def post_event(body: EventIn) -> dict:
    return event_service.record(body.device_id, body.event_type, body.payload, body.occurred_at)


@router.get("/events")
def get_events(
    limit: int = Query(50, ge=1, le=200),
    device_id: Optional[str] = None,
    event_type: Optional[str] = None,
) -> dict:
    events = event_service.list_events(limit=limit, device_id=device_id, event_type=event_type)
    return {"count": len(events), "events": events}
