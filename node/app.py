"""node/app.py — FastAPI app factory. Phase 3: production hardening.

Everything that was Phase 2's endpoint set is unchanged in shape and
behavior. What Phase 3 adds:
  - logging routed to a rotating file (application AND uvicorn's own
    request/error logs — see _setup_logging()), configurable via
    node/config.py
  - a lifespan hook that logs startup/shutdown and checkpoints the WAL
    on the way out, so on-disk state stays tidy even if the process is
    later force-killed (e.g. Android's low-memory killer)
  - exception handlers that turn a busy/unavailable SQLite database
    into a clean 503 instead of a bare 500, and log every unhandled
    exception with enough context to debug after the fact
"""
import logging
import sys
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from node import config
from database import connection as db, migrate
from database.connection import DatabaseBusyError, DatabaseUnavailableError
from services import health_service
from api import status as status_api
from api import health as health_api
from api import heartbeat as heartbeat_api
from api import events as events_api
from api import memory as memory_api
from api import timeline as timeline_api
from api import sync as sync_api

log = logging.getLogger("mayanode")

# Loggers that write every request/response and server-level errors.
# Captured into the same rotating file as our own "mayanode" logger so
# nothing is lost when Termux's session ends and stdout/stderr go away.
_UVICORN_LOGGERS = ("uvicorn.error", "uvicorn.access")


def _setup_logging() -> logging.Logger:
    """Attach a size-based rotating file handler to the app's own
    logger and to uvicorn's. Falls back to console-only logging (rather
    than crashing at startup) if the log directory can't be created or
    written to — storage permissions on Android/Termux can be flaky,
    and a logging problem should never be the reason the server won't
    start."""
    level = getattr(logging, config.LOG_LEVEL, None)
    if not isinstance(level, int):
        logging.basicConfig(level=logging.INFO)
        logging.getLogger("mayanode").warning(
            f"MAYANODE_LOG_LEVEL={config.LOG_LEVEL!r} is not a valid level, using INFO"
        )
        level = logging.INFO

    handlers: list[logging.Handler] = []
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            config.LOG_DIR / "node.log",
            maxBytes=config.LOG_MAX_BYTES, backupCount=config.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-7s  %(name)s — %(message)s")
        )
        handlers.append(file_handler)
    except OSError as e:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(name)s — %(message)s"))
        handlers.append(console)
        logging.getLogger("mayanode").warning(
            f"could not open log file under {config.LOG_DIR} ({e}); logging to stderr only"
        )

    for logger_name in ("mayanode", *_UVICORN_LOGGERS):
        logger = logging.getLogger(logger_name)
        logger.setLevel(level)
        logger.propagate = False
        # create_app() is safe to call more than once (e.g. tests that
        # build a fresh app per case) — close and drop any handlers a
        # previous call attached, rather than piling up open file
        # handles across calls.
        for old in list(logger.handlers):
            old.close()
            logger.removeHandler(old)
        for h in handlers:
            logger.addHandler(h)

    if not config.ACCESS_LOG:
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    return logging.getLogger("mayanode")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    log.info(f"{config.NAME} {config.VERSION} ready — listening on {config.HOST}:{config.PORT}")
    yield
    log.info(f"{config.NAME} shutting down")
    try:
        result = db.checkpoint_wal()
        log.info(f"WAL checkpoint on shutdown: {result}")
    except Exception as e:
        # Best-effort tidiness step — never let it block shutdown.
        log.warning(f"WAL checkpoint on shutdown failed (non-fatal): {e}")
    logging.shutdown()


def create_app() -> FastAPI:
    _setup_logging()

    try:
        applied = migrate.run()
        if applied:
            log.info(f"migrations applied on startup: {applied}")
        else:
            log.info("schema already up to date")
    except Exception:
        log.critical("startup aborted: database migrations failed", exc_info=True)
        raise

    health_service.register_check("sqlite", db.check)

    app = FastAPI(title=config.NAME, version=config.VERSION, lifespan=_lifespan)

    @app.exception_handler(DatabaseBusyError)
    async def _db_busy(request: Request, exc: DatabaseBusyError) -> JSONResponse:
        log.warning(f"{request.method} {request.url.path} — database busy: {exc}")
        return JSONResponse({"error": "database_busy", "detail": str(exc)}, status_code=503)

    @app.exception_handler(DatabaseUnavailableError)
    async def _db_unavailable(request: Request, exc: DatabaseUnavailableError) -> JSONResponse:
        log.error(f"{request.method} {request.url.path} — database unavailable: {exc}")
        return JSONResponse({"error": "database_unavailable", "detail": str(exc)}, status_code=503)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # A genuine bug: log the full traceback for after-the-fact
        # debugging, but never leak internals to the client.
        log.error(f"unhandled error on {request.method} {request.url.path}: {exc}", exc_info=True)
        return JSONResponse({"error": "internal_server_error"}, status_code=500)

    app.include_router(status_api.router)
    app.include_router(health_api.router)
    app.include_router(heartbeat_api.router)
    app.include_router(events_api.router)
    app.include_router(memory_api.router)
    app.include_router(timeline_api.router)
    app.include_router(sync_api.router)

    log.info(f"{config.NAME} {config.VERSION} app created")
    return app


app = create_app()
