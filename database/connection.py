"""database/connection.py — SQLite access. One short-lived connection per
operation (no shared connection across the server's worker threads).

Every connection gets two pragmas applied up front:
  - busy_timeout: belt-and-braces alongside sqlite3.connect(timeout=...)
    (the Python driver's timeout already retries internally on
    SQLITE_BUSY, but setting the pragma too means the same bound applies
    even if a future code path opens a connection some other way).
  - synchronous: see node/config.DB_SYNCHRONOUS's docstring.
"""
import logging
import sqlite3
from contextlib import contextmanager

from node import config

log = logging.getLogger("mayanode.db")


class DatabaseUnavailableError(RuntimeError):
    """Raised when SQLite couldn't be reached at all (e.g. the storage
    path is missing or unwritable) — distinct from DatabaseBusyError,
    which means the file was reachable but locked."""


class DatabaseBusyError(RuntimeError):
    """Raised when a write couldn't proceed because another connection
    held the lock past config.DB_TIMEOUT_S. Callers (see api layer) can
    turn this into a clean 503 instead of a bare 500."""


def connect() -> sqlite3.Connection:
    """Open one connection with this app's standard pragmas applied.

    Opening the file and applying the startup pragmas are both wrapped
    in the same error translation: sqlite3.connect() itself is lazy (it
    doesn't necessarily touch the file until the first statement runs),
    so a locked or unreachable database can just as easily surface on
    the PRAGMA calls below as on the open call — both paths need to
    come out as one of our two typed errors, not a bare
    sqlite3.OperationalError that callers have no clean way to handle.
    """
    conn = None
    try:
        conn = sqlite3.connect(config.DB_PATH, timeout=config.DB_TIMEOUT_S)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(config.DB_TIMEOUT_S * 1000)}")
        conn.execute(f"PRAGMA synchronous = {config.DB_SYNCHRONOUS}")
        return conn
    except sqlite3.OperationalError as e:
        if conn is not None:
            conn.close()
        msg = str(e).lower()
        if "locked" in msg or "busy" in msg:
            log.warning(f"database busy while opening a connection: {e}")
            raise DatabaseBusyError(str(e)) from e
        # e.g. the parent directory doesn't exist or isn't writable —
        # this is a setup/storage problem, not a lock contention one.
        log.error(f"could not open database at {config.DB_PATH}: {e}")
        raise DatabaseUnavailableError(str(e)) from e


@contextmanager
def connection():
    """Commit on success, roll back on any error, always close. A lock
    that couldn't be acquired within config.DB_TIMEOUT_S surfaces as
    DatabaseBusyError so callers can distinguish "the database is
    momentarily busy, retry" from a genuine bug."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except sqlite3.OperationalError as e:
        conn.rollback()
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            log.warning(f"database busy: {e}")
            raise DatabaseBusyError(str(e)) from e
        log.error(f"database operational error: {e}")
        raise
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
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            raise DatabaseBusyError(str(e)) from e
        raise
    finally:
        conn.rollback()
        conn.close()


def checkpoint_wal() -> dict:
    """Force pending WAL frames back into the main database file and
    truncate the WAL. Used on graceful shutdown (so the on-disk state is
    tidy even if the process is later force-killed) and before/after a
    backup. Safe to call at any time; a no-op if there's nothing pending."""
    conn = connect()
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        # (busy, log_frames, checkpointed_frames)
        return {"busy": row[0], "log_frames": row[1], "checkpointed_frames": row[2]}
    finally:
        conn.close()
