"""api/status.py — GET /status"""
from fastapi import APIRouter

from services import status_service

router = APIRouter()


@router.get("/status")
def status() -> dict:
    return status_service.get_status()
