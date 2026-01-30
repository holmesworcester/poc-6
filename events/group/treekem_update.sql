-- TreeKEM Phase 2: treekem_updates (update orchestration events)
-- Each update ties together an update path with leaf pubkey reference for atomicity
CREATE TABLE IF NOT EXISTS treekem_updates (
    treekem_update_id TEXT NOT NULL,
    author_peer_id TEXT NOT NULL,            -- peer_shared_id of the update author
    leaf_pubkey_shared_id TEXT NOT NULL,     -- Leaf pubkey_shared for this update (for atomicity)
    removal_epoch_id TEXT,                   -- Removal epoch for forward secrecy (NULL = initial)
    base_update_id TEXT,                     -- Previous update this builds on (NULL = first)
    created_at INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (treekem_update_id, recorded_by)
);

-- Index for finding updates by removal epoch
CREATE INDEX IF NOT EXISTS idx_treekem_updates_epoch
ON treekem_updates(removal_epoch_id, recorded_by, created_at DESC);

-- Index for finding concurrent updates (same base)
CREATE INDEX IF NOT EXISTS idx_treekem_updates_base
ON treekem_updates(base_update_id, removal_epoch_id, recorded_by);

-- Index for finding updates by author
CREATE INDEX IF NOT EXISTS idx_treekem_updates_author
ON treekem_updates(author_peer_id, recorded_by, created_at DESC);

-- TreeKEM winning state (deterministic convergence for concurrent updates)
-- The update with the lexicographically smallest treekem_update_id wins
-- Winner is computed at query time by sorting updates, not stored here.
-- This table is reserved for future use if we need to cache winning state.
CREATE TABLE IF NOT EXISTS treekem_tree_state (
    state_key TEXT NOT NULL,             -- "{removal_epoch_id}:{base_update_id}"
    winning_update_id TEXT NOT NULL,     -- The winning update's ID (computed, not inserted)
    removal_epoch_id TEXT,               -- For convenience
    base_update_id TEXT,                 -- For convenience
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (state_key, recorded_by)
);
