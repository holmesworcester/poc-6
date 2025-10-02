-- Schema for user events (network membership)
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    peer_id TEXT NOT NULL,
    name TEXT NOT NULL,
    joined_at INTEGER NOT NULL,
    invite_pubkey TEXT NOT NULL,
    UNIQUE(peer_id)
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    added_by TEXT NOT NULL,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (group_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_users_peer
ON users(peer_id);

CREATE INDEX IF NOT EXISTS idx_group_members_group
ON group_members(group_id);
