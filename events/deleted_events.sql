-- Deleted events tracking
-- Records which events have been deleted and should never be projected
-- Used to prevent projection of messages that arrive after their deletion
CREATE TABLE IF NOT EXISTS deleted_events (
    event_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    deleted_at INTEGER NOT NULL,
    PRIMARY KEY (event_id, recorded_by)
);

-- Index for checking if an event is deleted
CREATE INDEX IF NOT EXISTS idx_deleted_events_lookup
ON deleted_events(event_id, recorded_by);
