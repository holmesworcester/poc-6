-- Removed peers tracking (device-wide)
-- Marks peers (by peer_shared_id) as removed to prevent their sync requests
-- Removal is a global property known across the network
-- Historical events from removed peers remain valid
CREATE TABLE IF NOT EXISTS removed_peers (
    peer_shared_id TEXT NOT NULL PRIMARY KEY,
    removed_at INTEGER NOT NULL,
    removed_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_removed_peers_removed_at
ON removed_peers(removed_at);
