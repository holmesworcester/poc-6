-- Channels table for storing channel information
CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    group_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    seen_by_peer_id TEXT NOT NULL,
    received_at INTEGER NOT NULL
);

-- Index for querying channels in a group
CREATE INDEX IF NOT EXISTS idx_channels_group
ON channels(group_id);

-- Index for querying channels seen by a specific peer
CREATE INDEX IF NOT EXISTS idx_channels_seen_by
ON channels(seen_by_peer_id);
