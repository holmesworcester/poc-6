-- Shareable key events (symmetric keys sealed to recipient prekeys)
CREATE TABLE IF NOT EXISTS keys_shared (
    key_shared_id TEXT NOT NULL,
    original_key_id TEXT NOT NULL,  -- The key_id being shared
    created_by TEXT NOT NULL,        -- peer_shared_id of creator
    created_at INTEGER NOT NULL,
    recipient_peer_id TEXT NOT NULL, -- Who this key was sealed to
    recorded_by TEXT NOT NULL,   -- Who actually received it (should match recipient)
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (key_shared_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_keys_shared_by_peer
    ON keys_shared(recorded_by, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_keys_shared_by_key
    ON keys_shared(original_key_id, recorded_by);
