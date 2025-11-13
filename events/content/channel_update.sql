-- Channel updates table for tracking channel modifications
-- Records admin-initiated changes to channel name or disappearing_time_ms
-- Each peer has their own view of channel updates they've seen
CREATE TABLE IF NOT EXISTS channel_updates (
    update_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    updated_by TEXT NOT NULL,  -- peer_shared_id of the admin making the update
    global_count INTEGER NOT NULL,  -- For ordering updates to same channel (highest wins)
    new_channel_name TEXT,  -- NULL if name not changed
    new_disappearing_time_ms INTEGER,  -- NULL if disappearing_time not changed
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (update_id, recorded_by)
);

-- Index for finding updates to a specific channel
CREATE INDEX IF NOT EXISTS idx_channel_updates_channel
ON channel_updates(channel_id, recorded_by);

-- Index for querying updates by updater (admin)
CREATE INDEX IF NOT EXISTS idx_channel_updates_by_updater
ON channel_updates(updated_by, recorded_by);

-- Index for sorting by global_count (for winning update selection)
CREATE INDEX IF NOT EXISTS idx_channel_updates_count
ON channel_updates(channel_id, global_count DESC, recorded_by);
