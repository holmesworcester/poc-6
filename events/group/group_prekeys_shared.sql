-- Group prekeys shared table for asymmetric encryption of group messages
-- Stores public group prekeys from the network (peer-subjective)
-- Each peer sees different network members' group prekeys
-- group_prekey_id is used as hint in wrapped blobs (matches recipient's local prekey_id)

CREATE TABLE IF NOT EXISTS group_prekeys_shared (
    group_prekey_shared_id TEXT NOT NULL,
    group_prekey_id TEXT NOT NULL,  -- Links to group_prekeys table (used as crypto hint)
    peer_id TEXT NOT NULL,
    public_key BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    ttl_ms INTEGER NOT NULL DEFAULT 0,  -- Absolute time (ms since epoch) when expires. 0 = never
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (group_prekey_shared_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_group_prekeys_shared_peer
ON group_prekeys_shared(peer_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_group_prekeys_shared_ttl
ON group_prekeys_shared(ttl_ms) WHERE ttl_ms > 0;
