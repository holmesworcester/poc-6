-- Schema for user events (network membership)
-- Each peer has their own view of users they've seen
-- user_pubkey is user's OWN unique key (for verifying signed_by=user_id on first peer invite)
-- NOTE: user→peer relationship is in linked_peers table (one user can have many peers/devices)
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT NOT NULL,              -- Event hash of user event (person identity)
    name TEXT NOT NULL,
    network_id TEXT,
    created_at INTEGER NOT NULL,
    user_pubkey TEXT NOT NULL,  -- User's OWN keypair (NOT shared with invite)
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, recorded_by)
);

-- NOTE: User-to-peer relationship is now stored in peers_shared table (user_id column)
-- This replaces the old linked_peers join table for cleaner schema organization

-- NOTE: group_members table is defined in events/group/group_member.sql
-- The duplicate definition here was removed to avoid schema conflicts
