-- Local-only storage for peer identities controlled by this device
-- Contains private keys for signing (NOT for key exchange - see local_prekeys)
-- In tests: May contain multiple identities (Alice, Bob, Charlie)
-- In production: Typically one identity per device
CREATE TABLE IF NOT EXISTS local_peers (
    peer_id TEXT PRIMARY KEY,
    public_key TEXT NOT NULL,
    private_key BLOB NOT NULL,
    created_at INTEGER NOT NULL
);

-- Index for looking up by public key
CREATE INDEX IF NOT EXISTS idx_local_peers_pubkey
ON local_peers(public_key);
