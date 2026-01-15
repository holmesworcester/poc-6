-- Admin actions tracking table
-- Tracks all admin-gated events (invite, admin, group_member, channel) for chain validation
-- Used to find the latest admin action per peer for removal events
CREATE TABLE IF NOT EXISTS admin_actions (
    action_id TEXT NOT NULL,           -- The event ID (invite, admin, group_member, channel)
    action_type TEXT NOT NULL,         -- 'invite', 'admin', 'group_member', 'channel'
    peer_shared_id TEXT NOT NULL,      -- Which peer created this action
    prior_admin_action TEXT,           -- Chain link to previous action (NULL for first action)
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (action_id, recorded_by)
);

-- Index for finding latest action per peer (for removal event creation)
CREATE INDEX IF NOT EXISTS idx_admin_actions_peer
ON admin_actions(peer_shared_id, recorded_by, created_at DESC);

-- Index for walking the chain (for DAG ancestry validation)
CREATE INDEX IF NOT EXISTS idx_admin_actions_prior
ON admin_actions(prior_admin_action, recorded_by);
