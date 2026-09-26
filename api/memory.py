"""api/memory.py — PUT /memory (create/overwrite), GET /memory (read).

GET modes:
  device_id + key  -> one record (404 if absent)
  device_id only   -> every key under that device
  neither          -> every record currently stored (capped by limit)
  key without device_id -> 400 (a bare key is ambiguous across devices)
"""
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from services import memory_service

router = APIRouter()


class MemoryIn(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=64)
    key: str = Field(..., min_length=1, max_length=128)
    value: Any = None


@router.put("/memory")
def put_memory(body: MemoryIn) -> dict:
    return memory_service.set_value(body.device_id, body.key, body.value)


@router.get("/memory")
def get_memory(
    device_id: Optional[str] = None,
    key: Optional[str] = None,
    limit: int = Query(200, ge=1, le=500),
) -> dict:
    if key and not device_id:
        raise HTTPException(status_code=400, detail="key requires device_id")

    if device_id and key:
        record = memory_service.get_value(device_id, key)
        if record is None:
            raise HTTPException(status_code=404, detail="no such (device_id, key)")
        return record

    records = memory_service.list_values(device_id=device_id, limit=limit)
    return {"count": len(records), "records": records}
