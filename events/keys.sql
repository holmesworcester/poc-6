-- Local-only storage for symmetric encryption keys
CREATE TABLE IF NOT EXISTS keys (
    key_id TEXT PRIMARY KEY,
    key BLOB NOT NULL,
    created_at INTEGER NOT NULL
);

-- Track which local peers have access to each symmetric key
-- Supports multiple accounts on same device sharing network keys
-- Used for routing: when blob arrives with key_id, determines which local peer(s) to create recorded events for
-- NOTE: No FK constraint on peer_id to avoid issues with event ordering during reprojection
CREATE TABLE IF NOT EXISTS key_ownership (
    key_id TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (key_id, peer_id)
);

CREATE INDEX IF NOT EXISTS idx_key_ownership_key
ON key_ownership(key_id);
