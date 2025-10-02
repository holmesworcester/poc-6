-- Queue management tables for dependency resolution and incoming events

-- Track which events are valid (verified) for each peer
CREATE TABLE IF NOT EXISTS valid_events (
    event_id TEXT NOT NULL,
    seen_by_peer_id TEXT NOT NULL,
    PRIMARY KEY (event_id, seen_by_peer_id)
);

-- Blocked events per peer (blob already in store)
CREATE TABLE IF NOT EXISTS blocked_events (
    event_id TEXT NOT NULL,
    seen_by_peer_id TEXT NOT NULL,
    missing_deps TEXT NOT NULL,  -- JSON array of missing dep IDs
    PRIMARY KEY (event_id, seen_by_peer_id)
);

-- Dependency tracking for Kahn's algorithm
CREATE TABLE IF NOT EXISTS blocked_event_deps (
    event_id TEXT NOT NULL,
    seen_by_peer_id TEXT NOT NULL,
    dep_id TEXT NOT NULL,
    PRIMARY KEY (event_id, seen_by_peer_id, dep_id),
    FOREIGN KEY (event_id, seen_by_peer_id)
        REFERENCES blocked_events(event_id, seen_by_peer_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_blocked_deps_lookup
    ON blocked_event_deps(dep_id, seen_by_peer_id);

-- Incoming transit blobs queue
CREATE TABLE IF NOT EXISTS incoming_blobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    blob BLOB NOT NULL,
    sent_at INTEGER NOT NULL
);
