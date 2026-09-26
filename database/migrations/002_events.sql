CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',   -- JSON, stored as text
    occurred_at TEXT NOT NULL,                -- UTC ISO-8601
    received_at TEXT NOT NULL                 -- UTC ISO-8601, always server time
);
CREATE INDEX idx_events_occurred_at ON events(occurred_at);
CREATE INDEX idx_events_device_id   ON events(device_id);
