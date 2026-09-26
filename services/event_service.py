"""services/event_service.py — append-only event log.

No update/delete here by design: this is the substrate /timeline (and
later /memory) will read from, so history must never be rewritten. Event
semantics (what a given event_type means) live in callers, not here.

Sync support (seq, event_uid): every row is stamped with a value from the
shared monotonic sync_seq counter (database/sync_seq.py) at write time —
this is what /sync's cursor advances over. event_uid is an optional
client-supplied idempotency key (required only for sync-pushed events,
see record_for_sync); a plain POST /events leaves it NULL, which never
collides with a real key (the unique index is partial: WHERE event_uid
IS NOT NULL).
"""
import json
import sqlite3

from database.connection import connection
from database.sync_seq import next_seq
from node import clock

_COLUMNS = "id, device_id, event_type, payload, occurred_at, received_at, seq, event_uid"


def record(device_id: str, event_type: str, payload, occurred_at: str | None,
           event_uid: str | None = None) -> dict:
    """Append one event. occurred_at, if given, must already be a
    normalized UTC ISO-8601 string (see api/events.py's validator)."""
    received = clock.utc_now_iso()
    occurred = occurred_at or received
    with connection() as conn:
        seq = next_seq(conn)
        cur = conn.execute(
            "INSERT INTO events (device_id, event_type, payload, occurred_at, received_at, seq, event_uid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (device_id, event_type, json.dumps(payload), occurred, received, seq, event_uid),
        )
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM events WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return _to_dict(row)


def record_for_sync(device_id: str, event_type: str, payload, occurred_at: str,
                     event_uid: str) -> tuple[dict, bool]:
    """Insert one sync-pushed event, idempotent on event_uid. Returns
    (row, created) — created=False means event_uid already existed (a
    retried/duplicate push); the EXISTING row is returned unchanged
    rather than inserting a second copy. Race-safe: if two requests for
    the same event_uid land concurrently, the loser's INSERT hits the
    unique index and falls back to re-reading the winner's row instead
    of erroring."""
    with connection() as conn:
        received = clock.utc_now_iso()
        seq = next_seq(conn)   # allocated even on a duplicate — a harmless gap, never reused
        try:
            cur = conn.execute(
                "INSERT INTO events (device_id, event_type, payload, occurred_at, received_at, seq, event_uid) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (device_id, event_type, json.dumps(payload), occurred_at, received, seq, event_uid),
            )
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM events WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return _to_dict(row), True
        except sqlite3.IntegrityError:
            existing = conn.execute(
                f"SELECT {_COLUMNS} FROM events WHERE event_uid = ?", (event_uid,)
            ).fetchone()
            return _to_dict(existing), False


def list_events(limit: int = 50, device_id: str | None = None,
                 event_type: str | None = None) -> list[dict]:
    """Newest first (by occurred_at, id as tiebreaker). limit is clamped
    to [1, 200] here too, as a safety net under the API layer's own check."""
    limit = max(1, min(limit, 200))
    clauses, params = [], []
    if device_id:
        clauses.append("device_id = ?")
        params.append(device_id)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"SELECT {_COLUMNS} FROM events{where} ORDER BY occurred_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_to_dict(r) for r in rows]


def list_timeline(since: str | None = None, until: str | None = None,
                   limit: int = 50) -> list[dict]:
    """Chronological browse over occurred_at, newest first.

    since/until, if given, must already be normalized UTC ISO-8601
    strings (see api/timeline.py's validator) — both are inclusive
    bounds. No device/type filtering here by design: that's /events'
    job. limit clamped to [1, 200] here too, as a safety net under the
    API layer's own check.
    """
    limit = max(1, min(limit, 200))
    clauses, params = [], []
    if since:
        clauses.append("occurred_at >= ?")
        params.append(since)
    if until:
        clauses.append("occurred_at <= ?")
        params.append(until)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"SELECT {_COLUMNS} FROM events{where} ORDER BY occurred_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_to_dict(r) for r in rows]


def list_since(cursor: int, limit: int = 500) -> list[dict]:
    """Events with seq > cursor, OLDEST first — the shape /sync needs so
    a client can apply them in order and advance its cursor to the last
    (highest-seq) one delivered."""
    limit = max(1, min(limit, 1000))
    with connection() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM events WHERE seq > ? ORDER BY seq ASC LIMIT ?",
            (cursor, limit),
        ).fetchall()
    return [_to_dict(r) for r in rows]


def _to_dict(row) -> dict:
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    return d


def count_since(cursor: int) -> int:
    """Total events with seq > cursor, ignoring limit — used only to
    determine /sync's has_more, not returned to clients directly."""
    with connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM events WHERE seq > ?", (cursor,)
        ).fetchone()[0]
