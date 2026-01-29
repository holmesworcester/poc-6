-- TreeKEM removal epochs (tracks user/peer removals for forward secrecy)
-- Each epoch removes a single peer and optionally links to a parent epoch
CREATE TABLE IF NOT EXISTS removal_epochs (
    epoch_id TEXT NOT NULL,
    signed_by TEXT NOT NULL,
    removed_peer_id TEXT NOT NULL,  -- The peer being removed
    removed_user_id TEXT,           -- Optional user ID being removed
    parent_epoch_id TEXT,           -- Optional parent epoch for DAG
    created_at INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (epoch_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_removal_epochs_by_peer
ON removal_epochs(recorded_by, created_at DESC);

-- Entities (users/peers) removed in each epoch
CREATE TABLE IF NOT EXISTS removal_epoch_refs (
    epoch_id TEXT NOT NULL,
    removed_id TEXT NOT NULL,  -- user_id or peer_id being removed
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (epoch_id, removed_id, recorded_by),
    FOREIGN KEY (epoch_id, recorded_by) REFERENCES removal_epochs(epoch_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_removal_epoch_refs_by_removed
ON removal_epoch_refs(removed_id, recorded_by);

-- Parent epochs for DAG convergence
CREATE TABLE IF NOT EXISTS removal_epoch_parents (
    epoch_id TEXT NOT NULL,
    parent_epoch_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (epoch_id, parent_epoch_id, recorded_by),
    FOREIGN KEY (epoch_id, recorded_by) REFERENCES removal_epochs(epoch_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_removal_epoch_parents_by_parent
ON removal_epoch_parents(parent_epoch_id, recorded_by);
