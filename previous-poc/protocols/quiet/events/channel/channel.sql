-- Channels table for storing channel information
CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    network_id TEXT NOT NULL,
    name TEXT NOT NULL,
    creator_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    description TEXT
);