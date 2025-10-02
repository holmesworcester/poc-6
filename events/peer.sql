-- Local-only storage for peer events (identity private keys)
CREATE TABLE IF NOT EXISTS peers (
    peer_id TEXT PRIMARY KEY,
    public_key TEXT NOT NULL,
    private_key BLOB NOT NULL,
    prekey_private BLOB,
    created_at INTEGER NOT NULL
);

-- Index for looking up by public key
CREATE INDEX IF NOT EXISTS idx_peers_pubkey
ON peers(public_key);
