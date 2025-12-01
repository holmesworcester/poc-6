-- Message reaction deletions table for tracking deleted reactions
-- Each peer has their own view of which reactions have been deleted
-- Blocks projection of deleted reactions (deletion blocking)
CREATE TABLE IF NOT EXISTS message_reaction_deletions (
    deletion_id TEXT NOT NULL,
    reaction_id TEXT NOT NULL,
    deleted_by TEXT NOT NULL,           -- peer_shared_id who deleted the reaction
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (deletion_id, recorded_by)
);

-- Index for checking if a reaction is deleted (used during projection)
CREATE INDEX IF NOT EXISTS idx_message_reaction_deletions_reaction
ON message_reaction_deletions(reaction_id, recorded_by);

-- Index for querying deletions by deleter
CREATE INDEX IF NOT EXISTS idx_message_reaction_deletions_deleter
ON message_reaction_deletions(deleted_by);
