-- Store table for all event blobs
CREATE TABLE IF NOT EXISTS store (
    id TEXT PRIMARY KEY,
    blob BLOB NOT NULL,
    stored_at INTEGER NOT NULL
);
