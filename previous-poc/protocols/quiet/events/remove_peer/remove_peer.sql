-- Schema for remove_peer event type
-- Tracks removed peers to prevent them from syncing new messages
-- NOTE: Peer events remain in database for historical record

CREATE TABLE IF NOT EXISTS removed_peers (
    -- The peer ID being removed
    peer_id TEXT PRIMARY KEY,

    -- When the removal occurred
    removed_at INTEGER NOT NULL,

    -- Which peer issued the removal
    removed_by TEXT,

    -- Optional reason for removal
    reason TEXT
);

-- Index for efficient timestamp-based queries
CREATE INDEX IF NOT EXISTS idx_removed_peers_time ON removed_peers(removed_at);
