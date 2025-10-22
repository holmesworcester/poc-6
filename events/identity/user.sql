-- Schema for user events (network membership)
-- Each peer has their own view of users they've seen
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    invite_pubkey TEXT NOT NULL,
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
