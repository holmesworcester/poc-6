-- Messages table for storing message events
-- Each peer has their own view of messages they've seen
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    seen_by_peer_id TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    PRIMARY KEY (message_id, seen_by_peer_id)
);

-- Index for querying messages in a channel by a specific peer
CREATE INDEX IF NOT EXISTS idx_messages_channel_peer
ON messages(channel_id, seen_by_peer_id, created_at DESC);

-- Index for querying messages by author
CREATE INDEX IF NOT EXISTS idx_messages_author
ON messages(author_id);
