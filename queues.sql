-- Queue management tables for dependency resolution and incoming events

-- Track which events are valid (verified) for each peer
CREATE TABLE IF NOT EXISTS valid_events (
    event_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (event_id, recorded_by)
);

-- Blocked events per peer (blob already in store) - EPHEMERAL
-- Note: _ephemeral suffix indicates this is temporary processing state that should
-- ideally be empty at the end (all events eventually unblock). If events remain blocked,
-- it may indicate a dependency resolution bug, but the exact count can vary by processing
-- order so we exclude from reprojection tests.
CREATE TABLE IF NOT EXISTS blocked_events_ephemeral (
    recorded_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    missing_deps TEXT NOT NULL,  -- JSON array of missing dep IDs
    deps_remaining INTEGER NOT NULL DEFAULT 0,  -- Count for Kahn's algorithm
    PRIMARY KEY (recorded_id, recorded_by)
);
CREATE INDEX IF NOT EXISTS idx_blocked_events_ephemeral_peer
    ON blocked_events_ephemeral(recorded_by);

-- Dependency tracking for Kahn's algorithm - EPHEMERAL
CREATE TABLE IF NOT EXISTS blocked_event_deps_ephemeral (
    recorded_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    dep_id TEXT NOT NULL,
    PRIMARY KEY (recorded_id, recorded_by, dep_id),
    FOREIGN KEY (recorded_id, recorded_by)
        REFERENCES blocked_events_ephemeral(recorded_id, recorded_by)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_blocked_deps_ephemeral_lookup
    ON blocked_event_deps_ephemeral(dep_id, recorded_by);

-- Incoming transit blobs queue
CREATE TABLE IF NOT EXISTS incoming_blobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    blob BLOB NOT NULL,
    sent_at INTEGER NOT NULL
);
