-- Connection prekeys shared table for asymmetric encryption of initial connection requests
-- Stores public connection prekeys from the network (peer-subjective)
-- Each peer sees different network members' connection prekeys
-- connection_prekey_shared_id is the event ID, used as hint in wrapped blobs

CREATE TABLE IF NOT EXISTS connection_prekeys_shared (
    connection_prekey_shared_id TEXT NOT NULL,
    connection_prekey_id TEXT NOT NULL,  -- Links to connection_prekeys table (foreign local dep)
    peer_id TEXT NOT NULL,  -- peer_shared_id (public identity)
    public_key BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    ttl_ms INTEGER NOT NULL DEFAULT 0,  -- Absolute time (ms since epoch) when expires. 0 = never
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (connection_prekey_shared_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_connection_prekeys_shared_peer
ON connection_prekeys_shared(peer_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_connection_prekeys_shared_ttl
ON connection_prekeys_shared(ttl_ms) WHERE ttl_ms > 0;
