"""services/status_service.py — builds the /status payload."""
import platform
import sqlite3
import time
from datetime import datetime, timezone

from node import config

_started_monotonic = time.monotonic()
_started_at = datetime.now(timezone.utc)


def get_status() -> dict:
    return {
        "name": config.NAME,
        "version": config.VERSION,
        "status": "ok",
        "started_at": _started_at.isoformat(),
        "uptime_seconds": round(time.monotonic() - _started_monotonic, 1),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
    }
