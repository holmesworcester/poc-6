CREATE TABLE IF NOT EXISTS file_slices (
    file_id TEXT NOT NULL,
    slice_number INTEGER NOT NULL,
    nonce BLOB NOT NULL,
    ciphertext BLOB NOT NULL,
    poly_tag BLOB NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (file_id, slice_number, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_file_slices_file_peer
ON file_slices(file_id, recorded_by, slice_number);
