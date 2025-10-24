CREATE TABLE IF NOT EXISTS message_attachments (
    message_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    filename TEXT,
    mime_type TEXT,

    -- File descriptor fields (was in separate 'file' event, now here)
    blob_bytes INTEGER NOT NULL,
    nonce_prefix BLOB NOT NULL,
    enc_key BLOB NOT NULL,
    root_hash BLOB NOT NULL,
    total_slices INTEGER NOT NULL,

    created_at INTEGER NOT NULL DEFAULT 0,
    ttl_ms INTEGER NOT NULL DEFAULT 0,  -- Absolute time (ms since epoch) when expires. 0 = never
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (message_id, file_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_message_attachments_message
ON message_attachments(message_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_message_attachments_file
ON message_attachments(file_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_message_attachments_ttl
ON message_attachments(ttl_ms) WHERE ttl_ms > 0;
