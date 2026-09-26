"""node/config.py — MayaNode settings. Kept as plain constants for now."""
import os
from pathlib import Path

NAME = "MayaNode"
VERSION = "0.1.0"

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"

# Loopback by default (no auth yet). For LAN testing later:
#   MAYANODE_HOST=0.0.0.0 python -m node
HOST = os.environ.get("MAYANODE_HOST", "127.0.0.1")
PORT = int(os.environ.get("MAYANODE_PORT", "8000"))

# SQLite
DB_PATH = Path(os.environ.get("MAYANODE_DB", BASE_DIR / "database" / "mayanode.db"))
MIGRATIONS_DIR = BASE_DIR / "database" / "migrations"
DB_TIMEOUT_S = 2.0   # max wait on a locked database (also bounds the health check)
