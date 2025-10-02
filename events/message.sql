-- Messages table for storing message events
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    seen_by_peer_id TEXT NOT NULL,
    received_at INTEGER NOT NULL
);

-- Index for querying messages in a channel by a specific peer
CREATE INDEX IF NOT EXISTS idx_messages_channel_peer
ON messages(channel_id, seen_by_peer_id, created_at DESC);

-- Index for querying messages by author
CREATE INDEX IF NOT EXISTS idx_messages_author
ON messages(author_id);
