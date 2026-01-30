-- TreeKEM pubkeys (local-only keypair storage for sender key distribution)
-- Each peer stores their own keypairs here (private keys survive replay)
-- Pattern: pubkey (local) + pubkey_shared (shareable)
CREATE TABLE IF NOT EXISTS pubkeys (
    pubkey_id TEXT NOT NULL,
    owner_peer_id TEXT NOT NULL,  -- peer_id who owns this keypair
    public_key BLOB NOT NULL,     -- 32-byte Ed25519 public key
    private_key BLOB NOT NULL,    -- 32-byte Ed25519 private key
    created_at INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (pubkey_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_pubkeys_owner
ON pubkeys(owner_peer_id, recorded_by, created_at DESC);
