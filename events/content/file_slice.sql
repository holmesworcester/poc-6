CREATE TABLE IF NOT EXISTS file_slices (
    file_id TEXT NOT NULL,
    slice_number INTEGER NOT NULL,
    nonce BLOB NOT NULL,
    ciphertext BLOB NOT NULL,
    poly_tag BLOB NOT NULL,
    event_id TEXT,  -- Event ID of the file_slice event (for sync_file)
    created_at INTEGER NOT NULL DEFAULT 0,
    ttl_ms INTEGER NOT NULL DEFAULT 0,  -- Absolute time (ms since epoch) when expires. 0 = never
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    -- Consolidated blob offset information (set during consolidation)
    ciphertext_len INTEGER,  -- Exact ciphertext length (fixes AEAD size variance)
    blob_offset_start INTEGER,  -- Byte offset where this slice starts in consolidated blob
    blob_offset_end INTEGER,  -- Byte offset where this slice ends in consolidated blob
    PRIMARY KEY (file_id, slice_number, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_file_slices_file_peer
ON file_slices(file_id, recorded_by, slice_number);

CREATE INDEX IF NOT EXISTS idx_file_slices_ttl
ON file_slices(ttl_ms) WHERE ttl_ms > 0;
