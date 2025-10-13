-- Group keys for network content encryption (subjective)
-- Each peer has their own view of which group keys they have access to
CREATE TABLE IF NOT EXISTS group_keys (
    key_id TEXT NOT NULL,
    key BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,  -- Which peer has access to this key
    PRIMARY KEY (key_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_group_keys_recorded_by
ON group_keys(recorded_by);
