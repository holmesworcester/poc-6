-- Prekeys shared table for asymmetric encryption of initial sync requests
-- Stores public prekeys from the network (peer-subjective)
-- Each peer sees different network members' prekeys
-- prekey_shared_id is the event ID, used as hint in wrapped blobs

CREATE TABLE IF NOT EXISTS prekeys_shared (
    prekey_shared_id TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    public_key BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (prekey_shared_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_prekeys_shared_peer
ON prekeys_shared(peer_id, recorded_by);
