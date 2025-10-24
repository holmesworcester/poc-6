-- Messages table for storing message events
-- Each peer has their own view of messages they've seen
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    ttl_ms INTEGER NOT NULL DEFAULT 0,  -- Absolute time (ms since epoch) when expires. 0 = never
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (message_id, recorded_by)
);

-- Index for querying messages in a channel by a specific peer
CREATE INDEX IF NOT EXISTS idx_messages_channel_peer
ON messages(channel_id, recorded_by, created_at DESC);

-- Index for querying messages by author
CREATE INDEX IF NOT EXISTS idx_messages_author
ON messages(author_id);

-- Index for querying expired messages
CREATE INDEX IF NOT EXISTS idx_messages_ttl
ON messages(ttl_ms) WHERE ttl_ms > 0;
