"""database/migrate.py — apply database/migrations/NNN_name.sql in order.

Each file runs in one transaction together with its schema_migrations row, so
a failed migration leaves nothing half-applied. Migration files must NOT
contain BEGIN/COMMIT themselves. Applied migrations are never re-run: to
change the schema, add a new numbered file instead of editing an old one.
"""
import logging
import re

from node import config, clock
from database.connection import connect

log = logging.getLogger("mayanode.migrate")

_NAME_RE = re.compile(r"^\d{3}_[a-z0-9_]+\.sql$")


def run() -> list[str]:
    """Apply pending migrations; return the versions applied on this call.

    Ensures config.DB_PATH's parent directory exists first, so a custom
    MAYANODE_DB pointed at a not-yet-created directory (a fresh install,
    or a path on external storage) works rather than failing with a
    confusing "unable to open database file"."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    files = sorted(config.MIGRATIONS_DIR.glob("*.sql"))
    bad = [f.name for f in files if not _NAME_RE.match(f.name)]
    if bad:
        raise RuntimeError(f"migration files must be named NNN_name.sql: {bad}")

    conn = connect()
    applied_now: list[str] = []
    current_version = None
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode.lower() != "wal":
            log.warning(f"WAL not available, journal_mode={mode}")

        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}

        for f in files:
            version = f.stem   # safe to inline: validated by _NAME_RE above
            if version in done:
                continue
            current_version = version
            sql = f.read_text(encoding="utf-8")
            conn.executescript(
                f"BEGIN;\n{sql}\n;\n"
                f"INSERT INTO schema_migrations (version, applied_at) "
                f"VALUES ('{version}', '{clock.utc_now_iso()}');\n"
                f"COMMIT;"
            )
            applied_now.append(version)
            log.info(f"applied migration {version}")
    except BaseException as e:
        conn.rollback()   # discard a half-run migration transaction
        if current_version:
            log.critical(f"migration {current_version} failed and was rolled back: {e}")
        else:
            log.critical(f"migration startup failed before any file ran: {e}")
        raise
    finally:
        conn.close()
    return applied_now
