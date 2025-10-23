CREATE TABLE IF NOT EXISTS networks (
    network_id TEXT NOT NULL,
    all_users_group_id TEXT NOT NULL,
    admins_group_id TEXT NOT NULL,
    creator_user_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (network_id, recorded_by)
);
