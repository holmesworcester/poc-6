-- Shareable key events (symmetric keys sealed to recipient prekeys)
-- Note: recipient identity is in the crypto hint, not stored in event data
CREATE TABLE IF NOT EXISTS group_keys_shared (
    key_shared_id TEXT NOT NULL,
    original_key_id TEXT NOT NULL,  -- The key_id being shared
    created_by TEXT NOT NULL,        -- peer_shared_id of creator
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,       -- Who decrypted and projected this event
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (key_shared_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_group_keys_shared_by_peer
    ON group_keys_shared(recorded_by, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_group_keys_shared_by_key
    ON group_keys_shared(original_key_id, recorded_by);
