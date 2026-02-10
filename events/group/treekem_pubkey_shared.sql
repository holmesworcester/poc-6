-- TreeKEM pubkeys shared: Received pubkeys from other peers (subjective)
-- Each peer stores pubkeys received from other group members

CREATE TABLE IF NOT EXISTS treekem_pubkeys_shared (
    treekem_pubkey_shared_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    depth INTEGER NOT NULL,
    path_prefix BLOB NOT NULL,
    public_key BLOB NOT NULL,
    owner_peer_shared_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    global_count INTEGER NOT NULL DEFAULT 0,
    ttl_ms INTEGER NOT NULL,
    signed_by TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (treekem_pubkey_shared_id, recorded_by)
);

-- Index for looking up pubkeys by group and node position
CREATE INDEX IF NOT EXISTS idx_treekem_pubkeys_shared_group_node
ON treekem_pubkeys_shared(group_id, depth, path_prefix, recorded_by);

-- Index for finding valid pubkeys for a specific owner
CREATE INDEX IF NOT EXISTS idx_treekem_pubkeys_shared_owner
ON treekem_pubkeys_shared(owner_peer_shared_id, group_id, recorded_by);

-- Index for TTL-based cleanup
CREATE INDEX IF NOT EXISTS idx_treekem_pubkeys_shared_ttl
ON treekem_pubkeys_shared(recorded_by, ttl_ms);

-- Index for cleanup queries
CREATE INDEX IF NOT EXISTS idx_treekem_pubkeys_shared_recorded_by
ON treekem_pubkeys_shared(recorded_by);
