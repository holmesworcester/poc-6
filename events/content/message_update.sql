-- Message updates table for tracking message edits
-- Each peer has their own view of message updates they've seen
-- Uses global_count for convergent ordering (highest wins)
CREATE TABLE IF NOT EXISTS message_updates (
    update_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    edited_by TEXT NOT NULL,       -- peer_shared_id of the editor (must be message author)
    author_id TEXT NOT NULL,       -- user_id (for authorization check - must match original)
    global_count INTEGER NOT NULL, -- For ordering updates to same message (highest wins)
    new_content TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (update_id, recorded_by)
);

-- Index for finding updates to a specific message
CREATE INDEX IF NOT EXISTS idx_message_updates_message
ON message_updates(message_id, recorded_by);

-- Index for winning update selection (global_count DESC, update_id DESC as tiebreaker)
CREATE INDEX IF NOT EXISTS idx_message_updates_winner
ON message_updates(message_id, recorded_by, global_count DESC, update_id DESC);

-- Index for finding updates by editor
CREATE INDEX IF NOT EXISTS idx_message_updates_editor
ON message_updates(edited_by, recorded_by);
