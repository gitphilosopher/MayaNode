"""node/config.py — MayaNode settings, centralized here as the single
place that reads the process environment. Everything else in the
codebase imports these names rather than touching os.environ directly.

Every setting has a safe default suitable for the reference deployment
(a Redmi Note 4 running Termux), so `python -m node` works with zero
configuration. Override any of it with environment variables, or by
dropping a `.env` file (KEY=VALUE per line) next to this repository's
root — see `_load_dotenv()` below. Real process environment variables
always win over `.env`, and `.env` is optional; nothing breaks if it's
absent.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal, dependency-free .env loader. Only sets a variable if it
    is not already present in the environment, so real env vars (e.g.
    ones set by systemd, Termux:Boot, or the shell) always take
    priority over the file. Silently does nothing if the file is
    missing or unreadable — a missing .env is normal, not an error."""
    try:
        if not path.is_file():
            return
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        # Read-only or inaccessible filesystem (can happen on Android
        # storage) — fall back to whatever real env vars are set.
        pass


_load_dotenv(BASE_DIR / ".env")


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name}={raw!r} is not a valid integer") from None


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"{name}={raw!r} is not a valid number") from None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ── Identity ─────────────────────────────────────────────────────────────

NAME = "MayaNode"
VERSION = "0.2.0"

# ── Network ──────────────────────────────────────────────────────────────

# Loopback by default (no auth yet). For LAN testing later:
#   MAYANODE_HOST=0.0.0.0 python -m node
HOST = _env_str("MAYANODE_HOST", "127.0.0.1")
PORT = _env_int("MAYANODE_PORT", 8000)

# uvicorn's graceful-shutdown grace period (seconds): how long in-flight
# requests get to finish once a shutdown signal (SIGINT/SIGTERM) arrives
# before the server forces the connection closed.
GRACEFUL_TIMEOUT_S = _env_float("MAYANODE_GRACEFUL_TIMEOUT_S", 10.0)

# ── SQLite ───────────────────────────────────────────────────────────────

DB_PATH = Path(_env_str("MAYANODE_DB", str(BASE_DIR / "database" / "mayanode.db")))
MIGRATIONS_DIR = BASE_DIR / "database" / "migrations"

# Max seconds a connection waits on a locked database before raising
# sqlite3.OperationalError ("database is locked"). Also bounds /health.
DB_TIMEOUT_S = _env_float("MAYANODE_DB_TIMEOUT_S", 5.0)

# WAL + synchronous=NORMAL is SQLite's own recommended durable-but-fast
# combination: still crash-safe (a crash mid-write can lose at most the
# last few not-yet-checkpointed commits, never corrupt the file), but
# far fewer fsync() calls than synchronous=FULL — worth it on a phone's
# flash storage. Overridable for anyone who wants FULL durability at the
# cost of write latency.
DB_SYNCHRONOUS = _env_str("MAYANODE_DB_SYNCHRONOUS", "NORMAL").upper()
if DB_SYNCHRONOUS not in ("OFF", "NORMAL", "FULL", "EXTRA"):
    raise RuntimeError(
        f"MAYANODE_DB_SYNCHRONOUS={DB_SYNCHRONOUS!r} must be one of OFF, NORMAL, FULL, EXTRA"
    )

# ── Logging ──────────────────────────────────────────────────────────────

LOG_DIR = Path(_env_str("MAYANODE_LOG_DIR", str(BASE_DIR / "logs")))
LOG_LEVEL = _env_str("MAYANODE_LOG_LEVEL", "INFO").upper()
LOG_MAX_BYTES = _env_int("MAYANODE_LOG_MAX_BYTES", 1_000_000)
LOG_BACKUP_COUNT = _env_int("MAYANODE_LOG_BACKUP_COUNT", 3)
# Uvicorn's per-request access log is useful for diagnostics but doubles
# the log volume; keep it on by default (storage is cheap relative to
# LOG_MAX_BYTES/LOG_BACKUP_COUNT capping total size) but let it be
# switched off on a very storage-constrained device.
ACCESS_LOG = _env_bool("MAYANODE_ACCESS_LOG", True)

# ── Backup ───────────────────────────────────────────────────────────────

BACKUP_DIR = Path(_env_str("MAYANODE_BACKUP_DIR", str(BASE_DIR / "database" / "backups")))
# How many backup files database/backup.py keeps before pruning the
# oldest. 0 disables pruning (keep everything).
BACKUP_RETENTION = _env_int("MAYANODE_BACKUP_RETENTION", 14)
