-- TreeKEM secrets_shared (symmetric keys sealed to recipient pubkeys)
-- Tracks which secrets have been shared with which recipients
CREATE TABLE IF NOT EXISTS secrets_shared (
    secret_shared_id TEXT NOT NULL,
    original_secret_id TEXT NOT NULL,  -- The secret_id being shared
    recipient_pubkey_id TEXT NOT NULL, -- The pubkey used for wrapping
    removal_epoch_id TEXT,             -- The removal epoch this key is valid for (NULL = initial)
    recorded_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (secret_shared_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_secrets_shared_by_peer
    ON secrets_shared(recorded_by, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_secrets_shared_by_secret
    ON secrets_shared(original_secret_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_secrets_shared_by_pubkey
    ON secrets_shared(recipient_pubkey_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_secrets_shared_by_epoch
    ON secrets_shared(removal_epoch_id, recorded_by);
