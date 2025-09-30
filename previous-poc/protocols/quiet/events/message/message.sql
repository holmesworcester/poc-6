-- Messages table for storing messages
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    network_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    reply_to TEXT -- Optional reference to another message
);