-- Channels table for storing channel information
-- Each peer has their own view of channels they've seen
CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT NOT NULL,
    name TEXT NOT NULL,
    group_id TEXT NOT NULL,
    signed_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    disappearing_time_ms INTEGER NOT NULL DEFAULT 0,  -- 0 = permanent, >0 = milliseconds until expiration
    is_main INTEGER DEFAULT 0,  -- 1 if this is the peer's main channel
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (channel_id, recorded_by)
);

-- Index for querying channels in a group
CREATE INDEX IF NOT EXISTS idx_channels_group
ON channels(group_id);

-- Index for querying channels seen by a specific peer
CREATE INDEX IF NOT EXISTS idx_channels_seen_by
ON channels(recorded_by);
