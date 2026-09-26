"""services/memory_service.py — durable per-device key/value state.

Deliberately NOT a semantic memory system: no importance/topic/mem_type,
no retrieval ranking. One (device_id, key) pair holds exactly one current
value; MayaVE (or whatever else) owns semantic interpretation — this is
just the persistence hub.

Two write paths:
  set_value()      — used by PUT /memory. Unconditional overwrite, server
                      always assigns updated_at = now. Behavior unchanged
                      from before sync support existed.
  sync_set_value() — used by POST /sync. Client supplies updated_at (the
                      time the value was set ON THE CLIENT, which may be
                      well before the sync request itself, e.g. an
                      offline edit) and the write is only accepted if
                      it's newer than what's already stored — see its
                      docstring for the full conflict rule.

Both paths stamp seq (from the shared sync_seq counter) and revision (a
per-key counter, 1 on first write, +1 on every accepted overwrite) so a
sync client can detect what changed and a future conflict strategy has
more to work with than a bare timestamp.
"""
import json

from database.connection import connection
from database.sync_seq import next_seq
from node import clock

_COLUMNS = "device_id, key, value, updated_at, seq, revision, updated_by"


def set_value(device_id: str, key: str, value) -> dict:
    """Create or overwrite one (device_id, key) record. Always wins —
    this is a direct, authoritative local write, not a sync negotiation."""
    now = clock.utc_now_iso()
    with connection() as conn:
        current = conn.execute(
            "SELECT revision FROM memory WHERE device_id = ? AND key = ?",
            (device_id, key),
        ).fetchone()
        revision = (current["revision"] + 1) if current else 1
        seq = next_seq(conn)
        conn.execute(
            """
            INSERT INTO memory (device_id, key, value, updated_at, seq, revision, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id, key) DO UPDATE SET
                value      = excluded.value,
                updated_at = excluded.updated_at,
                seq        = excluded.seq,
                revision   = excluded.revision,
                updated_by = excluded.updated_by
            """,
            (device_id, key, json.dumps(value), now, seq, revision, device_id),
        )
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM memory WHERE device_id = ? AND key = ?",
            (device_id, key),
        ).fetchone()
    return _to_dict(row)


def sync_set_value(device_id: str, key: str, value, updated_at: str, updated_by: str) -> tuple[dict, str]:
    """Apply one sync-pushed memory write with deterministic last-write-
    wins, compared on the CLIENT-supplied updated_at — not server receipt
    time — so two offline edits are ordered by when they actually
    happened, not by which one happened to sync first.

    updated_at must already be a normalized UTC ISO-8601 string (fixed-
    width, via clock.to_iso — see api/sync.py's validator), so plain
    string comparison is chronological comparison.

    Returns (record, status):
      "accepted" — no prior row, or incoming updated_at is strictly newer
      "noop"     — identical (updated_at, value) already stored; a safe,
                   idempotent retry — NOT reported as a conflict
      "conflict" — incoming is older, or ties on updated_at with a
                   different value (two writes at the same instant is
                   inherently ambiguous). The STORED record is left
                   untouched and returned so the caller can tell the
                   client what actually won.
    Values are never merged — the loser is simply not applied.
    """
    with connection() as conn:
        current = conn.execute(
            f"SELECT {_COLUMNS} FROM memory WHERE device_id = ? AND key = ?",
            (device_id, key),
        ).fetchone()

        if current is None:
            seq = next_seq(conn)
            conn.execute(
                "INSERT INTO memory (device_id, key, value, updated_at, seq, revision, updated_by) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (device_id, key, json.dumps(value), updated_at, seq, updated_by),
            )
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM memory WHERE device_id = ? AND key = ?",
                (device_id, key),
            ).fetchone()
            return _to_dict(row), "accepted"

        current_d = _to_dict(current)

        if updated_at == current_d["updated_at"]:
            if current_d["value"] == value:
                return current_d, "noop"
            return current_d, "conflict"

        if updated_at < current_d["updated_at"]:
            return current_d, "conflict"

        seq = next_seq(conn)
        conn.execute(
            "UPDATE memory SET value = ?, updated_at = ?, seq = ?, revision = revision + 1, updated_by = ? "
            "WHERE device_id = ? AND key = ?",
            (json.dumps(value), updated_at, seq, updated_by, device_id, key),
        )
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM memory WHERE device_id = ? AND key = ?",
            (device_id, key),
        ).fetchone()
        return _to_dict(row), "accepted"


def get_value(device_id: str, key: str) -> dict | None:
    """One record, or None if it doesn't exist."""
    with connection() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM memory WHERE device_id = ? AND key = ?",
            (device_id, key),
        ).fetchone()
    return _to_dict(row) if row else None


def list_values(device_id: str | None = None, limit: int = 200) -> list[dict]:
    """All records for a device, or (if device_id is None) every record
    currently stored, most recently updated first. limit clamped to
    [1, 500] here too, as a safety net under the API layer's own check."""
    limit = max(1, min(limit, 500))
    if device_id:
        query = f"SELECT {_COLUMNS} FROM memory WHERE device_id = ? ORDER BY updated_at DESC LIMIT ?"
        params = (device_id, limit)
    else:
        query = f"SELECT {_COLUMNS} FROM memory ORDER BY updated_at DESC LIMIT ?"
        params = (limit,)
    with connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_to_dict(r) for r in rows]


def list_since(cursor: int, limit: int = 500) -> list[dict]:
    """Memory rows with seq > cursor, OLDEST first — same shape as
    event_service.list_since(), for /sync."""
    limit = max(1, min(limit, 1000))
    with connection() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM memory WHERE seq > ? ORDER BY seq ASC LIMIT ?",
            (cursor, limit),
        ).fetchall()
    return [_to_dict(r) for r in rows]


def _to_dict(row) -> dict:
    d = dict(row)
    d["value"] = json.loads(d["value"])
    return d


def count_since(cursor: int) -> int:
    """Total memory rows with seq > cursor, ignoring limit — used only to
    determine /sync's has_more, not returned to clients directly."""
    with connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM memory WHERE seq > ?", (cursor,)
        ).fetchone()[0]
