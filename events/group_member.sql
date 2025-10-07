-- Group membership events - each peer's view of who is in each group (WIP version)
-- Members can only be added by existing members (or group creator for bootstrap)
CREATE TABLE IF NOT EXISTS group_members_wip (
    member_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,           -- peer_shared_id being added to the group
    added_by TEXT NOT NULL,           -- peer_shared_id who added them
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,        -- who recorded this event
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (member_id, recorded_by)
);

-- Index for querying members of a specific group (scoped to peer)
CREATE INDEX IF NOT EXISTS idx_group_members_wip_group
ON group_members_wip(group_id, recorded_by);

-- Index for querying which groups a user is in (scoped to peer)
CREATE INDEX IF NOT EXISTS idx_group_members_wip_user
ON group_members_wip(user_id, recorded_by);

-- Index for checking specific membership (scoped to peer)
CREATE INDEX IF NOT EXISTS idx_group_members_wip_lookup
ON group_members_wip(group_id, user_id, recorded_by);
