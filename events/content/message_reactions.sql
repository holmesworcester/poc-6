-- Message reactions table for storing emoji reactions to messages
-- Each peer has their own view of reactions they've seen
-- Uses global_count for deterministic ordering when multiple reactions tie
CREATE TABLE IF NOT EXISTS message_reactions (
    reaction_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    reactor_id TEXT NOT NULL,           -- user_id who reacted
    signed_by TEXT NOT NULL,            -- peer_shared_id who signed the reaction
    emoji TEXT NOT NULL,                -- Unicode emoji character(s)
    created_at INTEGER NOT NULL,
    global_count INTEGER NOT NULL,      -- Lamport clock for deterministic LWW ordering
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (reaction_id, recorded_by)
);

-- Index for querying reactions on a specific message
CREATE INDEX IF NOT EXISTS idx_message_reactions_message
ON message_reactions(message_id, recorded_by);

-- Index for querying reactions by a specific reactor
CREATE INDEX IF NOT EXISTS idx_message_reactions_reactor
ON message_reactions(reactor_id, recorded_by);

-- Index for LWW: partition by (message_id, reactor_id, emoji), order by global_count DESC
CREATE INDEX IF NOT EXISTS idx_message_reactions_lww
ON message_reactions(message_id, reactor_id, emoji, global_count DESC);
