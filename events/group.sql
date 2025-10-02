-- Groups table for storing group information
-- Each peer has their own view of groups they've seen
CREATE TABLE IF NOT EXISTS groups (
    group_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    key_id TEXT NOT NULL,
    seen_by_peer_id TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    PRIMARY KEY (group_id, seen_by_peer_id)
);

-- Index for querying groups seen by a specific peer
CREATE INDEX IF NOT EXISTS idx_groups_seen_by
ON groups(seen_by_peer_id);

-- Index for looking up by key_id
CREATE INDEX IF NOT EXISTS idx_groups_key
ON groups(key_id);
