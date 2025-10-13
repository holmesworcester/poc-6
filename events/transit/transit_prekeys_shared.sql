-- Transit prekeys shared table for asymmetric encryption of initial sync requests
-- Stores public transit prekeys from the network (peer-subjective)
-- Each peer sees different network members' transit prekeys
-- transit_prekey_shared_id is the event ID, used as hint in wrapped blobs

CREATE TABLE IF NOT EXISTS transit_prekeys_shared (
    transit_prekey_shared_id TEXT NOT NULL,
    transit_prekey_id TEXT NOT NULL,  -- Links to transit_prekeys table (foreign local dep)
    peer_id TEXT NOT NULL,  -- peer_shared_id (public identity)
    public_key BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (transit_prekey_shared_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_transit_prekeys_shared_peer
ON transit_prekeys_shared(peer_id, recorded_by);
