-- TreeKEM key shared: Keys encrypted to tree nodes (subjective)
-- Each peer stores keys received via TreeKEM distribution

CREATE TABLE IF NOT EXISTS treekem_keys_shared (
    treekem_key_shared_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    depth INTEGER NOT NULL,
    path_prefix BLOB NOT NULL,
    signed_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (treekem_key_shared_id, recorded_by)
);

-- Index for looking up keys by group
CREATE INDEX IF NOT EXISTS idx_treekem_keys_shared_group
ON treekem_keys_shared(group_id, recorded_by);

-- Index for looking up by key_id
CREATE INDEX IF NOT EXISTS idx_treekem_keys_shared_key
ON treekem_keys_shared(key_id, recorded_by);

-- Index for cleanup queries
CREATE INDEX IF NOT EXISTS idx_treekem_keys_shared_recorded_by
ON treekem_keys_shared(recorded_by);

-- Index for stats queries
CREATE INDEX IF NOT EXISTS idx_treekem_keys_shared_stats
ON treekem_keys_shared(recorded_by, created_at);
