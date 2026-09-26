"""api/health.py — GET /health  (200 = healthy, 503 = a check failed)"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services import health_service

router = APIRouter()


@router.get("/health")
def health() -> JSONResponse:
    # Plain `def`: FastAPI runs it in a worker thread, so future blocking
    # checks (SQLite) won't stall the event loop.
    result = health_service.run_checks()
    code = 200 if result["status"] == "ok" else 503
    return JSONResponse(result, status_code=code)
