CREATE TABLE memory (
    device_id  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,   -- JSON, stored as text
    updated_at TEXT NOT NULL,   -- UTC ISO-8601, always server time
    PRIMARY KEY (device_id, key)
);
CREATE INDEX idx_memory_device_id ON memory(device_id);
