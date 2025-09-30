-- Peers table for storing public peer information
CREATE TABLE IF NOT EXISTS peers (
    -- The peer ID (hash of peer event)
    peer_id TEXT PRIMARY KEY,

    -- The public key of the local peer (peer_secret) this peer represents
    public_key TEXT NOT NULL,

    -- The local peer id (hash of peer_secret event)
    peer_secret_id TEXT NOT NULL,

    -- When the peer was created
    created_at INTEGER NOT NULL
);

-- Index for looking up by peer_secret_id
CREATE INDEX IF NOT EXISTS idx_peers_peer_secret
ON peers(peer_secret_id);
