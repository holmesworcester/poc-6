-- Schema for invite events (peer-subjective)
-- Each peer has their own view of invites they've seen
-- Supports both mode='user' (joining network) and mode='link' (device linking)
CREATE TABLE IF NOT EXISTS invites (
    invite_id TEXT NOT NULL,
    invite_pubkey TEXT NOT NULL,
    group_id TEXT NOT NULL,
    inviter_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'user' CHECK (mode IN ('user', 'link')),
    user_id TEXT,                 -- For mode='link': target user to link to
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (invite_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_invites_pubkey
ON invites(invite_pubkey, recorded_by);

CREATE INDEX IF NOT EXISTS idx_invites_recorded_by
ON invites(recorded_by);
