-- TreeKEM Phase 2: treekem_secrets (tree node symmetric keys)
-- Each peer has their own view of which tree node secrets they have access to
CREATE TABLE IF NOT EXISTS treekem_secrets (
    treekem_secret_id TEXT NOT NULL,
    depth INTEGER NOT NULL,         -- Tree depth (0 = root)
    path_prefix BLOB NOT NULL,      -- Path prefix bytes (may be empty for root)
    key BLOB NOT NULL,              -- 32-byte symmetric key
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (treekem_secret_id, recorded_by)
);

-- Index for looking up secrets by tree position
CREATE INDEX IF NOT EXISTS idx_treekem_secrets_position
ON treekem_secrets(depth, path_prefix, recorded_by, created_at DESC);

-- Index for listing by peer
CREATE INDEX IF NOT EXISTS idx_treekem_secrets_recorded_by
ON treekem_secrets(recorded_by);
