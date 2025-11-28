-- Username updates: encrypted name updates for users
-- Each peer has their own view of user names (subjective table)
-- Stores the latest decrypted username and metadata
CREATE TABLE IF NOT EXISTS user_names (
    user_id TEXT NOT NULL,
    name TEXT,                      -- decrypted username
    encrypted_blob BLOB,            -- encrypted name if not yet decrypted
    event_id TEXT NOT NULL,         -- username_update event ID
    global_count INTEGER NOT NULL,  -- for LWW (last-writer-wins)
    key_id TEXT,                    -- group key used for encryption
    created_at INTEGER NOT NULL,
    signed_by TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, recorded_by)
    -- Note: Foreign key constraint omitted due to subjective scoping
    -- (users table has different recorded_by scope)
);

-- Index for querying user names (scoped to peer)
CREATE INDEX IF NOT EXISTS idx_user_names_by_recorded
    ON user_names(recorded_by, recorded_at DESC);

-- Index for checking if user has a name
CREATE INDEX IF NOT EXISTS idx_user_names_by_user
    ON user_names(user_id, recorded_by);
