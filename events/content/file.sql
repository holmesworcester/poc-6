CREATE TABLE IF NOT EXISTS files (
    file_id TEXT NOT NULL,
    blob_bytes INTEGER NOT NULL,
    nonce_prefix BLOB NOT NULL,
    enc_key BLOB NOT NULL,
    root_hash BLOB NOT NULL,
    total_slices INTEGER NOT NULL,
    created_at INTEGER NOT NULL DEFAULT 0,
    ttl_ms INTEGER NOT NULL DEFAULT 0,  -- Absolute time (ms since epoch) when expires. 0 = never
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (file_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_files_peer
ON files(recorded_by, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_files_ttl
ON files(ttl_ms) WHERE ttl_ms > 0;
