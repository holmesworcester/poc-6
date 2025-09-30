-- Local-only storage for peer_secret events (identity private keys)
CREATE TABLE IF NOT EXISTS peer_secrets (
    peer_secret_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    public_key TEXT NOT NULL,
    private_key BLOB NOT NULL,
    created_at INTEGER NOT NULL
);

-- Lookup by public key for signing
CREATE INDEX IF NOT EXISTS idx_peer_secrets_pubkey
ON peer_secrets(public_key);
