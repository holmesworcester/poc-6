-- TreeKEM pubkeys (local-only keypair storage for tree nodes)
-- Each peer stores their own keypairs here (private keys survive replay)
-- Pattern: treekem_pubkey (local) + treekem_pubkey_shared (shareable)
CREATE TABLE IF NOT EXISTS treekem_pubkeys (
    treekem_pubkey_id TEXT NOT NULL,
    owner_peer_id TEXT NOT NULL,  -- peer_id who owns this keypair
    public_key BLOB NOT NULL,     -- 32-byte Ed25519 public key
    private_key BLOB NOT NULL,    -- 32-byte Ed25519 private key
    created_at INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (treekem_pubkey_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_treekem_pubkeys_owner
ON treekem_pubkeys(owner_peer_id, recorded_by, created_at DESC);
