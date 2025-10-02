-- Shareable peer identity events (public keys visible to others)
CREATE TABLE IF NOT EXISTS peers_shared (
    peer_shared_id TEXT NOT NULL,
    public_key TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    seen_by_peer_id TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    PRIMARY KEY (peer_shared_id, seen_by_peer_id)
);

CREATE INDEX IF NOT EXISTS idx_peers_shared_by_peer
    ON peers_shared(seen_by_peer_id, received_at DESC);
