"""api/timeline.py — GET /timeline: chronological browse over events.

Distinct from GET /events: no device_id/event_type filtering (that's
/events' job) — this is purely a time-windowed, newest-first view.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from node import clock
from services import event_service

router = APIRouter()


def _normalize_bound(name: str, v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    try:
        dt = clock.parse_iso(v)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} must be a valid ISO-8601 datetime")
    if dt.tzinfo is None:
        raise HTTPException(status_code=422, detail=f"{name} must include a UTC offset")
    return clock.to_iso(dt)


@router.get("/timeline")
def get_timeline(
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    since_n = _normalize_bound("since", since)
    until_n = _normalize_bound("until", until)
    if since_n and until_n and since_n > until_n:
        raise HTTPException(status_code=422, detail="since must not be after until")

    events = event_service.list_timeline(since=since_n, until=until_n, limit=limit)
    return {"count": len(events), "since": since_n, "until": until_n, "events": events}
