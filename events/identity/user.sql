-- Schema for user events (network membership)
-- Each peer has their own view of users they've seen
-- Phase 2: user_pubkey is user's OWN unique key (for verifying signed_by=user_id on first peer invite)
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    name TEXT NOT NULL,
    network_id TEXT,
    created_at INTEGER NOT NULL,
    user_pubkey TEXT NOT NULL,  -- User's OWN keypair (NOT shared with invite)
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, recorded_by)
);

-- NOTE: group_members table is defined in events/group/group_member.sql
-- The duplicate definition here was removed to avoid schema conflicts

CREATE INDEX IF NOT EXISTS idx_users_peer
ON users(peer_id, recorded_by);
