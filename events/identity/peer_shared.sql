-- Shareable peer identity events (public keys visible to others)
-- user_id is set if this peer_shared was created via invite(mode='peer'), NULL for bootstrap self-signed
CREATE TABLE IF NOT EXISTS peers_shared (
    peer_shared_id TEXT NOT NULL,
    peer_id TEXT NOT NULL,  -- Local peer_id (for owner's view only)
    public_key TEXT NOT NULL,
    user_id TEXT,           -- User this peer belongs to (if linked via invite mode='peer'), NULL if bootstrap
    device_name TEXT,       -- Device name (e.g., "Phone", "Desktop")
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (peer_shared_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_peers_shared_by_peer
    ON peers_shared(recorded_by, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_peers_shared_by_peer_id
    ON peers_shared(peer_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_peers_shared_by_user_id
    ON peers_shared(user_id, recorded_by);
