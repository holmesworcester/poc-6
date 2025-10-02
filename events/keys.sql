-- Local-only storage for symmetric encryption keys
CREATE TABLE IF NOT EXISTS keys (
    key_id TEXT PRIMARY KEY,
    key BLOB NOT NULL,
    created_at INTEGER NOT NULL
);
