-- Admin actions tracking table
-- Tracks all admin-gated events (invite, admin, group_member, channel, user_removed) for DAG validation
-- Used to find the latest admin action per peer and walk the DAG for ancestry checks
CREATE TABLE IF NOT EXISTS admin_actions (
    action_id TEXT NOT NULL,           -- The event ID
    action_type TEXT NOT NULL,         -- 'invite', 'admin', 'group_member', 'channel', 'user_removed'
    peer_shared_id TEXT NOT NULL,      -- Which peer created this action
    prior_0 TEXT,                      -- prior[0]: own chain link (NULL for first action)
    prior_1 TEXT,                      -- prior[1]: optional merge reference
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (action_id, recorded_by)
);

-- Index for finding latest action per peer (for building chains)
CREATE INDEX IF NOT EXISTS idx_admin_actions_peer
ON admin_actions(peer_shared_id, recorded_by, created_at DESC);

-- Index for walking the DAG via prior[0] (device chain)
CREATE INDEX IF NOT EXISTS idx_admin_actions_prior_0
ON admin_actions(prior_0, recorded_by);

-- Index for walking the DAG via prior[1] (merge references)
CREATE INDEX IF NOT EXISTS idx_admin_actions_prior_1
ON admin_actions(prior_1, recorded_by);
