"""services/health_service.py — operational health checks for /health.

A check is a zero-argument function: it passes if it returns normally
(optionally returning a dict of extra details) and fails if it raises.
Other modules add checks with register_check(), e.g.:

    health_service.register_check("sqlite", database.check)
"""
import logging
import time
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("mayanode.health")

CheckFn = Callable[[], "dict | None"]

_checks: dict[str, CheckFn] = {}


def register_check(name: str, fn: CheckFn) -> None:
    """Add (or replace) a named health check."""
    _checks[name] = fn


def _app_check() -> None:
    """The process is up and able to run code. Always passes."""
    return None


register_check("app", _app_check)


def run_checks() -> dict:
    results: dict[str, dict] = {}
    healthy = True

    for name, fn in _checks.items():
        t0 = time.perf_counter()
        try:
            detail = fn() or {}
            result = {"ok": True, **detail}
        except Exception as e:
            healthy = False
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
            log.warning(f"health check '{name}' failed: {result['error']}")
        result["duration_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        results[name] = result

    return {
        "status": "ok" if healthy else "unhealthy",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": results,
    }
