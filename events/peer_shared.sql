-- Shareable peer identity events (public keys visible to others)
CREATE TABLE IF NOT EXISTS peers_shared (
    peer_shared_id TEXT NOT NULL,
    public_key TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (peer_shared_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_peers_shared_by_peer
    ON peers_shared(recorded_by, recorded_at DESC);
