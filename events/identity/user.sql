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

-- NOTE: group_members table is defined in events/group/group_member.sql
-- The duplicate definition here was removed to avoid schema conflicts

-- To find peers for a user, query linked_peers table
-- To find user for a peer_shared_id, query: SELECT user_id FROM linked_peers WHERE peer_id = ?
