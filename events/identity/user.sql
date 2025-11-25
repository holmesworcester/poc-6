-- Schema for user events (network membership)
-- Each peer has their own view of users they've seen
-- Phase 2: user_pubkey is user's OWN unique key (for verifying signed_by=user_id on first peer invite)
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT NOT NULL,              -- Event hash of user event (person identity)
    peer_id TEXT NOT NULL,              -- MISLEADING NAME: Actually stores peer_shared_id (shareable device ID)
    name TEXT NOT NULL,
    network_id TEXT,
    created_at INTEGER NOT NULL,
    user_pubkey TEXT NOT NULL,  -- User's OWN keypair (NOT shared with invite)
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, recorded_by)
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    added_by TEXT NOT NULL,
    added_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (group_id, user_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_users_peer
ON users(peer_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_group_members_group
ON group_members(group_id, recorded_by);
