-- Subjective mapping from local peer_id to shareable peer_shared_id and user_id
-- Each peer has their own view with just their single identity mapping
-- This allows querying peer_shared_id and user_id from peer_id without accessing device-wide tables
-- user_id is set when the account is created (user.create) or linked (link.create)
CREATE TABLE IF NOT EXISTS peer_self (
    peer_id TEXT NOT NULL,
    peer_shared_id TEXT NOT NULL,
    user_id TEXT,  -- Set when link/user is created - "what user am I?"
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (peer_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_peer_self_by_recorded
ON peer_self(recorded_by);
