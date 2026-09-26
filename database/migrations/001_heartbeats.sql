CREATE TABLE heartbeats (
    device_id  TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,   -- UTC ISO-8601
    last_seen  TEXT NOT NULL,   -- UTC ISO-8601
    beat_count INTEGER NOT NULL DEFAULT 1
);
