-- TreeKEM key requests (requests for secrets from other peers)
-- Tracks who requested what and whether we've responded
CREATE TABLE IF NOT EXISTS key_requests (
    request_id TEXT NOT NULL,
    requested_secret_id TEXT NOT NULL,
    removal_epoch_id TEXT,  -- The epoch context of the request
    requester_peer_id TEXT NOT NULL,  -- Who is asking
    created_at INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL,
    fulfilled INTEGER NOT NULL DEFAULT 0,  -- 1 if we've responded
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (request_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_key_requests_by_secret
ON key_requests(requested_secret_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_key_requests_unfulfilled
ON key_requests(recorded_by, fulfilled) WHERE fulfilled = 0;

CREATE INDEX IF NOT EXISTS idx_key_requests_by_requester
ON key_requests(requester_peer_id, recorded_by);
