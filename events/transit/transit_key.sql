-- Transit keys for sync responses and invite routing (device-wide)
-- These are temporary keys created for routing responses back to the correct peer
CREATE TABLE IF NOT EXISTS transit_keys (
    key_id TEXT PRIMARY KEY,
    key BLOB NOT NULL,
    owner_peer_id TEXT NOT NULL,  -- Which local peer owns this transit key
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transit_keys_owner
ON transit_keys(owner_peer_id);
