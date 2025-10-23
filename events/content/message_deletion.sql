-- Message deletions table for tracking deleted messages
-- Each peer has their own view of which messages have been deleted
-- Acts like blocking: if deletion exists, message projection is skipped
CREATE TABLE IF NOT EXISTS message_deletions (
    deletion_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    deleted_by TEXT NOT NULL,  -- peer_shared_id who created the deletion
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (message_id, recorded_by)  -- Idempotent: one deletion per message per peer
);

-- Index for querying deletions by message (for checking before message projection)
CREATE INDEX IF NOT EXISTS idx_message_deletions_message
ON message_deletions(message_id, recorded_by);

-- Index for querying deletions by deleter
CREATE INDEX IF NOT EXISTS idx_message_deletions_deleter
ON message_deletions(deleted_by);
