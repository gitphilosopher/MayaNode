-- Adds sync support: a shared monotonic sequence across events and memory
-- (the /sync cursor), an idempotency key for events, and conflict
-- metadata for memory. Existing rows are backfilled so every row has a
-- seq; relative order between the two tables' PRE-EXISTING rows carries
-- no meaning (nothing has synced before this migration) — only "unique
-- and strictly increasing" matters for the cursor to work correctly.

ALTER TABLE events ADD COLUMN seq INTEGER;
ALTER TABLE events ADD COLUMN event_uid TEXT;

ALTER TABLE memory ADD COLUMN seq INTEGER;
ALTER TABLE memory ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE memory ADD COLUMN updated_by TEXT;

CREATE TABLE sync_seq (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    value INTEGER NOT NULL DEFAULT 0
);
INSERT INTO sync_seq (id, value) VALUES (1, 0);

UPDATE events
SET seq = t.rn
FROM (SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM events) AS t
WHERE events.id = t.id;

UPDATE memory
SET seq = (SELECT COUNT(*) FROM events) + t.rn,
    updated_by = COALESCE(memory.updated_by, memory.device_id)
FROM (
    SELECT device_id, key, ROW_NUMBER() OVER (ORDER BY device_id, key) AS rn
    FROM memory
) AS t
WHERE memory.device_id = t.device_id AND memory.key = t.key;

UPDATE sync_seq
SET value = (
    SELECT COALESCE(MAX(x.seq), 0) FROM (
        SELECT seq FROM events UNION ALL SELECT seq FROM memory
    ) x
);

CREATE UNIQUE INDEX idx_events_seq ON events(seq);
CREATE UNIQUE INDEX idx_memory_seq ON memory(seq);
CREATE UNIQUE INDEX idx_events_uid ON events(event_uid) WHERE event_uid IS NOT NULL;
