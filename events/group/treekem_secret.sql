-- TreeKEM path secrets (local-only symmetric keys for path secret distribution)
-- Each peer has their own view of which path secrets they have access to
-- Pattern: treekem_secret (local) + treekem_secret_shared (shareable)
CREATE TABLE IF NOT EXISTS treekem_secrets (
    treekem_secret_id TEXT NOT NULL,
    depth INTEGER NOT NULL,             -- Tree depth (0 = root)
    path_prefix BLOB NOT NULL,          -- Path prefix bytes (up to 16 bytes)
    key BLOB NOT NULL,                  -- 32-byte symmetric key
    source_update_id TEXT,              -- treekem_update that created this secret (for purge tracking)
    recorded_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (treekem_secret_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_treekem_secrets_recorded_by
ON treekem_secrets(recorded_by);

CREATE INDEX IF NOT EXISTS idx_treekem_secrets_position
ON treekem_secrets(depth, path_prefix, recorded_by);

CREATE INDEX IF NOT EXISTS idx_treekem_secrets_source_update
ON treekem_secrets(source_update_id, recorded_by);
