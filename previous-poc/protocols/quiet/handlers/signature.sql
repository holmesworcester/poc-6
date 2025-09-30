-- Signing keys table for check_sig handler
CREATE TABLE IF NOT EXISTS signing_keys (
    peer_id TEXT PRIMARY KEY,
    network_id TEXT NOT NULL,
    private_key BLOB NOT NULL,
    created_at INTEGER NOT NULL
);