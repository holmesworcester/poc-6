-- Message rekey events for forward secrecy
-- Tracks re-encrypted messages when their original encryption key is being purged
CREATE TABLE IF NOT EXISTS message_rekeys (
    rekey_id TEXT NOT NULL,
    original_message_id TEXT NOT NULL,
    new_key_id TEXT NOT NULL,
    new_ciphertext BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (rekey_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_message_rekeys_by_message
    ON message_rekeys(original_message_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_message_rekeys_by_peer
    ON message_rekeys(recorded_by, created_at DESC);

-- Keys marked for purging (from deleted messages)
-- These keys encrypted deleted messages and should be purged along with their content
CREATE TABLE IF NOT EXISTS keys_to_purge (
    key_id TEXT NOT NULL,
    marked_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (key_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_keys_to_purge_by_peer
    ON keys_to_purge(recorded_by, marked_at DESC);
