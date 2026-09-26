"""database/connection.py — SQLite access. One short-lived connection per
operation (no shared connection across the server's worker threads)."""
import sqlite3
from contextlib import contextmanager

from node import config


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=config.DB_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def connection():
    """Commit on success, roll back on any error, always close."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def check() -> dict:
    """Health check: can we take the write lock and read the schema table?
    BEGIN IMMEDIATE proves the DB is writable and not stuck behind another
    writer (waits at most DB_TIMEOUT_S), then rolls back — no data changes."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        n = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        return {"migrations_applied": n}
    finally:
        conn.rollback()
        conn.close()
