-- Schema for remove handler
-- Tracks explicitly deleted events and removal context

CREATE TABLE IF NOT EXISTS deleted_events (
    event_id TEXT PRIMARY KEY,
    deleted_at INTEGER NOT NULL,
    deleted_by TEXT,  -- peer_id that deleted it
    reason TEXT       -- optional reason
);

CREATE TABLE IF NOT EXISTS deleted_channels (
    channel_id TEXT PRIMARY KEY,
    deleted_at INTEGER NOT NULL,
    deleted_by TEXT
);

CREATE TABLE IF NOT EXISTS removed_users (
    user_id TEXT PRIMARY KEY,
    removed_at INTEGER NOT NULL,
    removed_by TEXT
);

-- Indexes for efficient lookups
CREATE INDEX IF NOT EXISTS idx_deleted_events_type ON deleted_events(reason);
CREATE INDEX IF NOT EXISTS idx_deleted_channels_time ON deleted_channels(deleted_at);
CREATE INDEX IF NOT EXISTS idx_removed_users_time ON removed_users(removed_at);