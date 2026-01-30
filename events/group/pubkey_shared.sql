-- TreeKEM pubkeys_shared (shareable public keys for sender key distribution)
-- Each peer has their view of other peers' public keys for wrapping secrets
-- Pattern: pubkey (local) + pubkey_shared (shareable)
CREATE TABLE IF NOT EXISTS pubkeys_shared (
    pubkey_shared_id TEXT NOT NULL,
    pubkey_id TEXT NOT NULL,          -- Reference to local pubkey event
    owner_peer_id TEXT NOT NULL,      -- peer_shared_id who owns this pubkey
    public_key BLOB NOT NULL,         -- 32-byte Ed25519 public key
    created_at INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (pubkey_shared_id, recorded_by)
);

-- Index for looking up pubkeys by owner (for sender key distribution)
CREATE INDEX IF NOT EXISTS idx_pubkeys_shared_owner
ON pubkeys_shared(owner_peer_id, recorded_by, created_at DESC);

-- Index for looking up by pubkey_id (for key unwrapping)
CREATE INDEX IF NOT EXISTS idx_pubkeys_shared_pubkey_id
ON pubkeys_shared(pubkey_id, recorded_by);
