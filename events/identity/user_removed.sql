-- Removed users tracking (peer-scoped)
-- Marks users as removed from the network to prevent their sync requests
-- Each peer tracks their own view of removals
-- Historical events from removed users remain queryable (late joiners can still see them)
CREATE TABLE IF NOT EXISTS removed_users (
    user_id TEXT NOT NULL,
    removed_at INTEGER NOT NULL,
    removed_by TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_removed_users_recorded_by
ON removed_users(recorded_by);

CREATE INDEX IF NOT EXISTS idx_removed_users_removed_at
ON removed_users(removed_at, recorded_by);
