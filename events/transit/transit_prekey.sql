-- Transit prekeys for receiving sync requests (device-wide)
-- Each local peer has one transit prekey for initial contact
CREATE TABLE IF NOT EXISTS transit_prekeys (
    prekey_id TEXT PRIMARY KEY,
    owner_peer_id TEXT NOT NULL,  -- FK to local_peers.peer_id
    public_key BLOB NOT NULL,
    private_key BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (owner_peer_id) REFERENCES local_peers(peer_id)
);

CREATE INDEX IF NOT EXISTS idx_transit_prekeys_owner
ON transit_prekeys(owner_peer_id);
