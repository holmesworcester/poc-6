-- Message reactions table for emoji reactions to messages
-- Each peer has their own view of reactions on messages
-- Uses global_count for deterministic convergence when multiple devices react simultaneously
CREATE TABLE IF NOT EXISTS message_reactions (
    reaction_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    reactor_id TEXT NOT NULL,
    signed_by TEXT NOT NULL,
    emoji TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    global_count INTEGER NOT NULL,  -- For deterministic convergence (highest wins)
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (reaction_id, recorded_by),
    UNIQUE(message_id, reactor_id, emoji, recorded_by),
    FOREIGN KEY (message_id, recorded_by) REFERENCES messages(message_id, recorded_by)
);

-- Index for querying reactions by message
CREATE INDEX IF NOT EXISTS idx_message_reactions_message_id
ON message_reactions(message_id, recorded_by);

-- Index for querying reactions by reactor
CREATE INDEX IF NOT EXISTS idx_message_reactions_reactor_id
ON message_reactions(reactor_id, recorded_by);

-- Index for efficient duplicate detection and convergence
CREATE INDEX IF NOT EXISTS idx_message_reactions_combo
ON message_reactions(message_id, reactor_id, emoji, global_count DESC, recorded_by);

-- Message reaction deletions table for tracking deleted reactions (audit trail)
CREATE TABLE IF NOT EXISTS message_reaction_deletions (
    deletion_id TEXT NOT NULL,
    reaction_id TEXT NOT NULL,
    deleted_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (deletion_id, recorded_by),
    UNIQUE(reaction_id, recorded_by)
);

-- Index for querying deletions by reaction
CREATE INDEX IF NOT EXISTS idx_message_reaction_deletions_reaction_id
ON message_reaction_deletions(reaction_id, recorded_by);
