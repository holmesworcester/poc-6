-- Schema for user events
-- Users represent peers who have joined a network

CREATE TABLE IF NOT EXISTS users (
    -- Unique user ID from the event
    user_id TEXT PRIMARY KEY,

    -- The peer's identity ID
    peer_id TEXT NOT NULL,

    -- Network this user belongs to
    network_id TEXT NOT NULL,

    -- User's display name
    name TEXT NOT NULL,

    -- When the user joined
    joined_at INTEGER NOT NULL,

    -- The invite pubkey used to join
    invite_pubkey TEXT NOT NULL,

    -- Ensure one user event per peer per network
    UNIQUE(peer_id, network_id)
);

-- Index for querying users by network
CREATE INDEX IF NOT EXISTS idx_users_network 
ON users(network_id, joined_at DESC);

-- Index for looking up by peer_id
CREATE INDEX IF NOT EXISTS idx_users_peer 
ON users(peer_id);