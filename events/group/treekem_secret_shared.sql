-- TreeKEM path secrets shared (path secrets sealed to recipient treekem_pubkeys)
-- Tracks which path secrets have been shared to which copath nodes
CREATE TABLE IF NOT EXISTS treekem_secrets_shared (
    treekem_secret_shared_id TEXT NOT NULL,
    treekem_secret_id TEXT NOT NULL,       -- The path secret being shared
    recipient_pubkey_id TEXT NOT NULL,     -- The treekem_pubkey used for wrapping
    source_update_id TEXT,                 -- The treekem_update this secret is part of (optional)
    depth INTEGER NOT NULL,                -- Tree depth of the shared secret
    recorded_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (treekem_secret_shared_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_treekem_secrets_shared_by_peer
    ON treekem_secrets_shared(recorded_by, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_treekem_secrets_shared_by_secret
    ON treekem_secrets_shared(treekem_secret_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_treekem_secrets_shared_by_pubkey
    ON treekem_secrets_shared(recipient_pubkey_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_treekem_secrets_shared_by_update
    ON treekem_secrets_shared(source_update_id, recorded_by);
