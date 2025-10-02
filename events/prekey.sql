-- Pre-keys table for asymmetric encryption of initial sync requests

CREATE TABLE IF NOT EXISTS pre_keys (
    peer_id TEXT PRIMARY KEY,
    public_key BLOB NOT NULL,
    created_at INTEGER NOT NULL
);
