-- Groups table for storing group information
-- Each peer has their own view of groups they've seen
-- In sender key model, groups don't have encryption keys - messages use sender keys
CREATE TABLE IF NOT EXISTS groups (
    group_id TEXT NOT NULL,
    name TEXT NOT NULL,
    signed_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    is_main INTEGER DEFAULT 0,  -- 1 if this is the peer's main group for inviting
    network_id TEXT DEFAULT '',  -- Network this group belongs to (if any)
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (group_id, recorded_by)
);

-- Index for querying groups seen by a specific peer
CREATE INDEX IF NOT EXISTS idx_groups_seen_by
ON groups(recorded_by);

-- Index for looking up groups by network
CREATE INDEX IF NOT EXISTS idx_groups_network
ON groups(network_id, recorded_by);
