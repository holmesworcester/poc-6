-- Schema for link events (shareable)
-- Represents devices linked to the same user
CREATE TABLE IF NOT EXISTS linked_peers (
    link_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    linked_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (link_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_linked_peers_user_id
ON linked_peers(user_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_linked_peers_peer_id
ON linked_peers(peer_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_linked_peers_recorded_by
ON linked_peers(recorded_by);
