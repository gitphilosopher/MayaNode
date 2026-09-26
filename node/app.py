"""node/app.py — FastAPI app factory. Phase 2's full endpoint set."""
import logging
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI

from node import config
from database import connection as db, migrate
from services import health_service
from api import status as status_api
from api import health as health_api
from api import heartbeat as heartbeat_api
from api import events as events_api
from api import memory as memory_api
from api import timeline as timeline_api
from api import sync as sync_api


def _setup_logging() -> logging.Logger:
    config.LOG_DIR.mkdir(exist_ok=True)
    log = logging.getLogger("mayanode")
    log.setLevel(logging.INFO)
    if not log.handlers:
        handler = RotatingFileHandler(
            config.LOG_DIR / "node.log",
            maxBytes=1_000_000, backupCount=2, encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-7s  %(name)s — %(message)s")
        )
        log.addHandler(handler)
    return log


def create_app() -> FastAPI:
    log = _setup_logging()
    migrate.run()   # fail fast at startup if the schema can't be brought up to date
    health_service.register_check("sqlite", db.check)
    app = FastAPI(title=config.NAME, version=config.VERSION)
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
