"""node/__main__.py — run with:  python -m node


All tunables come from node/config.py (env-driven, see .env.example).
uvicorn's own logging is disabled here (log_config=None) because
node/app.py's _setup_logging() already attaches handlers directly to
the "uvicorn.error"/"uvicorn.access" loggers before the server starts
serving — letting uvicorn additionally apply its own default logging
config would either duplicate output or clobber those handlers.
"""

import uvicorn
from node import config

if __name__ == "__main__":
    uvicorn.run(
        "node.app:app",
        host=config.HOST,
        port=config.PORT,
        log_level=config.LOG_LEVEL.lower(),
        log_config=None,
        access_log=config.ACCESS_LOG,
        timeout_graceful_shutdown=int(config.GRACEFUL_TIMEOUT_S),
    )
