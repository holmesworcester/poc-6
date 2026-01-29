-- TreeKEM Phase 2: treekem_secrets_shared (tree node key distribution)
-- Tracks which tree node secrets have been shared with which recipients
CREATE TABLE IF NOT EXISTS treekem_secrets_shared (
    treekem_secret_shared_id TEXT NOT NULL,
    treekem_secret_id TEXT NOT NULL,     -- The treekem_secret_id being shared
    recipient_pubkey_id TEXT NOT NULL,   -- The treekem_pubkey used for wrapping
    source_update_id TEXT NOT NULL,      -- The treekem_update this is part of
    depth INTEGER NOT NULL,              -- Tree depth of the shared secret
    recorded_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (treekem_secret_shared_id, recorded_by)
);

-- Index for finding shares by update
CREATE INDEX IF NOT EXISTS idx_treekem_secrets_shared_update
ON treekem_secrets_shared(source_update_id, recorded_by);

-- Index for finding shares by secret
CREATE INDEX IF NOT EXISTS idx_treekem_secrets_shared_secret
ON treekem_secrets_shared(treekem_secret_id, recorded_by);

-- Index for finding shares by recipient
CREATE INDEX IF NOT EXISTS idx_treekem_secrets_shared_recipient
ON treekem_secrets_shared(recipient_pubkey_id, recorded_by);
