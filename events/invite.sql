-- Schema for invite events
CREATE TABLE IF NOT EXISTS invites (
    invite_id TEXT PRIMARY KEY,
    invite_pubkey TEXT NOT NULL UNIQUE,
    group_id TEXT NOT NULL,
    inviter_id TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_invites_pubkey
ON invites(invite_pubkey);
