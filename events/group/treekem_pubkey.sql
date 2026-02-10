-- TreeKEM pubkeys: Local keypairs at tree positions (subjective)
-- Each peer stores their own keypairs for nodes on their path to root

CREATE TABLE IF NOT EXISTS treekem_pubkeys (
    treekem_pubkey_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    depth INTEGER NOT NULL,
    path_prefix BLOB NOT NULL,
    public_key BLOB NOT NULL,
    private_key BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    ttl_ms INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (treekem_pubkey_id, recorded_by)
);

-- Index for looking up pubkeys by group and node position
CREATE INDEX IF NOT EXISTS idx_treekem_pubkeys_group_node
ON treekem_pubkeys(group_id, depth, path_prefix, recorded_by);

-- Index for finding pubkeys that need refresh (by TTL)
CREATE INDEX IF NOT EXISTS idx_treekem_pubkeys_ttl
ON treekem_pubkeys(recorded_by, ttl_ms);

-- Index for cleanup queries
CREATE INDEX IF NOT EXISTS idx_treekem_pubkeys_recorded_by
ON treekem_pubkeys(recorded_by);
