"""api/sync.py — POST /sync: bidirectional exchange of events and memory
between a client (e.g. MayaVE) and this MayaNode hub.

Stateless: every request is self-contained (device_id + cursor + payload,
see services/sync_service.py). No session, no auth yet — device_id is
trusted as given, which is acceptable for a first implementation on a
private network. Structured so auth can be layered in later (e.g. a
dependency that validates device_id against a request header/token)
without changing this shape.

Protocol version (Step 2 — see node/protocol.py and
docs/PROTOCOL_CONTRACT.md): every request declares a protocol_version
(defaulting to the current one, so an older payload with no such field
still validates); an unsupported version is rejected here as a clean
422, before it ever reaches sync_service — a whole-request-level
concern, distinct from per-event validation (event_type/payload shape),
which happens per-item inside sync_service so one malformed event never
fails an entire batch.
"""
from typing import Any, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field, validator

from node import clock
from node.protocol import PROTOCOL_VERSION, is_version_supported
from services import sync_service

router = APIRouter()


def _require_utc_iso(name: str, v: str) -> str:
    try:
        dt = clock.parse_iso(v)
    except ValueError:
        raise ValueError(f"{name} must be a valid ISO-8601 datetime")
    if dt.tzinfo is None:
        raise ValueError(f"{name} must include a UTC offset")
    return clock.to_iso(dt)


class SyncEventIn(BaseModel):
    event_uid: str = Field(..., min_length=1, max_length=128)
    event_type: str = Field(..., min_length=1, max_length=64)
    payload: Any = Field(default_factory=dict)
    occurred_at: str
    device_id: Optional[str] = Field(None, max_length=64)   # defaults to the request's device_id
    # Step 2 (docs/PROTOCOL_CONTRACT.md) — which version of event_type's
    # payload schema this event was produced against; None lets
    # node/protocol.py fall back to the registered type's current
    # schema_version.
    schema_version: Optional[int] = None

    @validator("occurred_at")
    def _v_occurred_at(cls, v):
        return _require_utc_iso("occurred_at", v)


class SyncMemoryIn(BaseModel):
    key: str = Field(..., min_length=1, max_length=128)
    value: Any = None
    updated_at: str
    device_id: Optional[str] = Field(None, max_length=64)   # defaults to the request's device_id

    @validator("updated_at")
    def _v_updated_at(cls, v):
        return _require_utc_iso("updated_at", v)


class SyncIn(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=64)
    cursor: int = Field(0, ge=0)
    limit: int = Field(500, ge=1, le=1000)
    events: List[SyncEventIn] = Field(default_factory=list)
    memory: List[SyncMemoryIn] = Field(default_factory=list)
    # Step 2 (docs/PROTOCOL_CONTRACT.md) — protocol version this client
    # speaks. Absent -> treated as version 1 by FastAPI's default here.
    protocol_version: int = Field(default=PROTOCOL_VERSION)

    @validator("protocol_version")
    def _v_protocol_version(cls, v):
        if not is_version_supported(v):
            raise ValueError(f"protocol_version {v!r} is not supported by this server")
        return v


@router.post("/sync")
def post_sync(body: SyncIn) -> dict:
    events = [
        {
            "device_id": e.device_id or body.device_id,
            "event_type": e.event_type,
            "payload": e.payload,
            "occurred_at": e.occurred_at,
            "event_uid": e.event_uid,
            "schema_version": e.schema_version,
        }
        for e in body.events
    ]
    memory = [
        {
            "device_id": m.device_id or body.device_id,
            "key": m.key,
            "value": m.value,
            "updated_at": m.updated_at,
        }
        for m in body.memory
    ]
    return sync_service.apply_sync(
        device_id=body.device_id, cursor=body.cursor,
        events=events, memory=memory, limit=body.limit,
    )
