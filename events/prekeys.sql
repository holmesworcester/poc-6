-- Local-only storage for prekeys owned by local peers
-- Contains private keys for key exchange (ephemeral keys, can be rotated)
-- Each prekey is owned by exactly one local peer
CREATE TABLE IF NOT EXISTS prekeys (
    prekey_id TEXT PRIMARY KEY,
    owner_peer_id TEXT NOT NULL,  -- FK to local_peers.peer_id
    public_key BLOB NOT NULL,
    private_key BLOB NOT NULL,
    prekey_shared_id TEXT,        -- Link to shareable prekey_shared event (for lookup)
    created_at INTEGER NOT NULL,
    FOREIGN KEY (owner_peer_id) REFERENCES local_peers(peer_id)
);

-- Index for looking up prekeys by owner
CREATE INDEX IF NOT EXISTS idx_prekeys_owner
ON prekeys(owner_peer_id);

-- Index for looking up by prekey_id and owner (for access control)
CREATE INDEX IF NOT EXISTS idx_prekeys_id_owner
ON prekeys(prekey_id, owner_peer_id);

-- Index for looking up by prekey_shared_id (for unwrapping blobs with prekey_shared_id hint)
CREATE INDEX IF NOT EXISTS idx_prekeys_shared_id
ON prekeys(prekey_shared_id, owner_peer_id);
