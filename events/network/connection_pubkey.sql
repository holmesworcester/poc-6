-- Connection pubkeys for receiving connection requests (device-wide)
-- Each local peer has connection pubkeys for initial contact
CREATE TABLE IF NOT EXISTS connection_pubkeys (
    connection_pubkey_id TEXT PRIMARY KEY,
    owner_peer_id TEXT NOT NULL,  -- FK to local_peers.peer_id
    public_key BLOB NOT NULL,
    private_key BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    ttl_ms INTEGER NOT NULL DEFAULT 0,  -- Absolute time (ms since epoch) when expires. 0 = never
    FOREIGN KEY (owner_peer_id) REFERENCES local_peers(peer_id)
);

CREATE INDEX IF NOT EXISTS idx_connection_pubkeys_owner
ON connection_pubkeys(owner_peer_id);

CREATE INDEX IF NOT EXISTS idx_connection_pubkeys_ttl
ON connection_pubkeys(ttl_ms) WHERE ttl_ms > 0;
