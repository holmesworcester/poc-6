-- Local-only storage for prekeys owned by local peers
-- Contains private keys for key exchange (ephemeral keys, can be rotated)
-- Each prekey is owned by exactly one local peer
CREATE TABLE IF NOT EXISTS local_prekeys (
    prekey_id TEXT PRIMARY KEY,
    owner_peer_id TEXT NOT NULL,  -- FK to local_peers.peer_id
    public_key BLOB NOT NULL,
    private_key BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (owner_peer_id) REFERENCES local_peers(peer_id)
);

-- Index for looking up prekeys by owner
CREATE INDEX IF NOT EXISTS idx_local_prekeys_owner
ON local_prekeys(owner_peer_id);

-- Index for looking up by prekey_id and owner (for access control)
CREATE INDEX IF NOT EXISTS idx_local_prekeys_id_owner
ON local_prekeys(prekey_id, owner_peer_id);
