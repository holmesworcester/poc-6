-- Admin event table: grants admin status to a user
-- This is a first-class event type, NOT a group

CREATE TABLE IF NOT EXISTS admins (
    admin_id TEXT NOT NULL,
    network_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    signed_by TEXT NOT NULL,      -- network_id (bootstrap) or peer_shared_id (ongoing)
    admin_grant TEXT,             -- NULL for bootstrap, prior admin_id for ongoing grants
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (admin_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_admins_user ON admins(user_id, network_id, recorded_by);
CREATE INDEX IF NOT EXISTS idx_admins_network ON admins(network_id, recorded_by);
