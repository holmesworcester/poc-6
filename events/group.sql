-- Groups table for storing group information
CREATE TABLE IF NOT EXISTS groups (
    group_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    key_id TEXT NOT NULL,
    seen_by_peer_id TEXT NOT NULL,
    received_at INTEGER NOT NULL
);

-- Index for querying groups seen by a specific peer
CREATE INDEX IF NOT EXISTS idx_groups_seen_by
ON groups(seen_by_peer_id);

-- Index for looking up by key_id
CREATE INDEX IF NOT EXISTS idx_groups_key
ON groups(key_id);
