-- Sync_file-related schema for prioritized file_slice syncing

-- Files wanted for active syncing (ephemeral, user-driven)
-- Tracks which files a peer wants to actively sync (e.g., user opened attachment)
CREATE TABLE IF NOT EXISTS file_sync_wanted (
    file_id TEXT NOT NULL,
    peer_id TEXT NOT NULL,              -- Local peer wanting to sync this file
    priority INTEGER NOT NULL DEFAULT 1, -- Higher = more urgent (1-10)
    ttl_ms INTEGER NOT NULL,            -- When to stop trying (0 = forever)
    requested_at INTEGER NOT NULL,
    PRIMARY KEY (file_id, peer_id)
);

CREATE INDEX IF NOT EXISTS idx_file_sync_wanted_peer
ON file_sync_wanted(peer_id, ttl_ms);

-- Per-file sync state tracking (ephemeral, system-maintained)
-- Tracks window-based sync progress for each file per peer pair
-- Note: _ephemeral suffix indicates this is scheduling/optimization state,
-- not canonical projection state.
CREATE TABLE IF NOT EXISTS file_sync_state_ephemeral (
    file_id TEXT NOT NULL,
    from_peer_id TEXT NOT NULL,         -- Local peer syncing
    to_peer_id TEXT NOT NULL,           -- Remote peer serving slices
    last_window INTEGER NOT NULL DEFAULT -1,  -- Last window synced (-1 = not started)
    w_param INTEGER NOT NULL,           -- Window parameter (bits) for this file
    slices_received INTEGER NOT NULL DEFAULT 0,
    total_slices INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (file_id, from_peer_id, to_peer_id)
);

CREATE INDEX IF NOT EXISTS idx_file_sync_state_ephemeral_file_from
ON file_sync_state_ephemeral(file_id, from_peer_id);

CREATE INDEX IF NOT EXISTS idx_file_sync_state_ephemeral_from_to
ON file_sync_state_ephemeral(from_peer_id, to_peer_id);
