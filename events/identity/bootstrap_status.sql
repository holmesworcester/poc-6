-- Bootstrap status projection (subjective)
-- Tracks whether a peer created or joined a network, and if they've received sync acknowledgment
CREATE TABLE IF NOT EXISTS bootstrap_status (
    peer_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    created_network INTEGER NOT NULL DEFAULT 0,  -- 1 if peer created network
    joined_network INTEGER NOT NULL DEFAULT 0,   -- 1 if peer joined network
    received_sync_request INTEGER NOT NULL DEFAULT 0,  -- 1 if joiner received first sync request
    inviter_peer_shared_id TEXT,  -- Set when joined_network=1
    PRIMARY KEY (peer_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_bootstrap_status_recorded_by
    ON bootstrap_status(recorded_by);
