-- Subjective mapping from local peer_id to shareable peer_shared_id
-- Each peer has their own view with just their single identity mapping
-- This allows querying peer_shared_id from peer_id without accessing device-wide tables
CREATE TABLE IF NOT EXISTS peer_self (
    peer_id TEXT NOT NULL,
    peer_shared_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (peer_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_peer_self_by_recorded
ON peer_self(recorded_by);
