-- Schema for link_invite events (peer-subjective)
-- Each peer has their own view of link invites they've created or received
CREATE TABLE IF NOT EXISTS link_invites (
    link_invite_id TEXT NOT NULL,
    link_pubkey TEXT NOT NULL,
    user_id TEXT NOT NULL,
    network_id TEXT,
    expiry_ms INTEGER,
    max_joiners INTEGER,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (link_invite_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_link_invites_user_id
ON link_invites(user_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_link_invites_recorded_by
ON link_invites(recorded_by);
