"""database/sync_seq.py — shared monotonic counter backing the /sync
cursor. next_seq() must be called with the SAME connection/transaction as
the row write it stamps, so the seq and the row become visible together.
"""


def next_seq(conn) -> int:
    conn.execute("UPDATE sync_seq SET value = value + 1 WHERE id = 1")
    return conn.execute("SELECT value FROM sync_seq WHERE id = 1").fetchone()[0]
