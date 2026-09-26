"""services/heartbeat_service.py — record and list device heartbeats."""
from database.connection import connection
from node import clock

_UPSERT = """
INSERT INTO heartbeats (device_id, first_seen, last_seen, beat_count)
VALUES (?, ?, ?, 1)
ON CONFLICT(device_id) DO UPDATE SET
    last_seen  = excluded.last_seen,
    beat_count = beat_count + 1
"""

_COLUMNS = "device_id, first_seen, last_seen, beat_count"


def record(device_id: str) -> dict:
    """Register a beat: create the device on first sight, else bump it."""
    now = clock.utc_now_iso()
    with connection() as conn:
        conn.execute(_UPSERT, (device_id, now, now))
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM heartbeats WHERE device_id = ?", (device_id,)
        ).fetchone()
    return dict(row)


def list_devices() -> list[dict]:
    """All known devices, most recently seen first."""
    with connection() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM heartbeats ORDER BY last_seen DESC"
        ).fetchall()
    now = clock.utc_now()
    devices = []
    for row in rows:
        d = dict(row)
        d["seconds_since_last_seen"] = round(
            (now - clock.parse_iso(d["last_seen"])).total_seconds(), 1
        )
        devices.append(d)
    return devices
