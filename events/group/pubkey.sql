-- TreeKEM pubkeys (shareable public keys for key distribution)
-- Each peer has their own view of pubkeys they can use for wrapping
CREATE TABLE IF NOT EXISTS pubkeys (
    pubkey_id TEXT NOT NULL,
    public_key BLOB NOT NULL,
    owner_peer_id TEXT NOT NULL,  -- peer_shared_id who created this pubkey
    created_at INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (pubkey_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_pubkeys_owner
ON pubkeys(owner_peer_id, recorded_by, created_at DESC);

-- Local-only secret storage for pubkey private keys
-- Only the owner has access to these (not synced)
CREATE TABLE IF NOT EXISTS pubkey_secrets (
    pubkey_id TEXT NOT NULL,
    private_key BLOB NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (pubkey_id, recorded_by)
);
