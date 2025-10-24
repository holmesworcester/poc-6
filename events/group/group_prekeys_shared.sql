-- Group prekeys shared table for asymmetric encryption of group messages
-- Stores public group prekeys from the network (peer-subjective)
-- Each peer sees different network members' group prekeys
-- group_prekey_shared_id is the event ID, used as hint in wrapped blobs

CREATE TABLE IF NOT EXISTS group_prekeys_shared (
    group_prekey_shared_id TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    public_key BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (group_prekey_shared_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_group_prekeys_shared_peer
ON group_prekeys_shared(peer_id, recorded_by);
