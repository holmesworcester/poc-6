-- Sync-related schema for bloom-based windowed sync protocol

-- Shareable events table: tracks which events can be synced to other peers
-- Each event has a window_id (computed from hash) for efficient windowed queries
CREATE TABLE IF NOT EXISTS shareable_events (
    event_id TEXT PRIMARY KEY,
    peer_id TEXT NOT NULL,           -- Peer who created this event (shareable identity)
    created_at INTEGER NOT NULL,
    window_id INTEGER                -- Computed from BLAKE2b-256(event_id) at w=20 for future-proofing
);

-- Index for querying shareable events by peer
CREATE INDEX IF NOT EXISTS idx_shareable_events_peer
    ON shareable_events(peer_id, created_at DESC);

-- Index for windowed sync queries (peer_id + window_id)
CREATE INDEX IF NOT EXISTS idx_shareable_events_window
    ON shareable_events(peer_id, window_id, created_at);

-- Sync state tracking per peer
-- Tracks window-based sync progress for each peer pair
CREATE TABLE IF NOT EXISTS sync_state (
    from_peer_id TEXT NOT NULL,      -- Local peer doing the syncing
    to_peer_id TEXT NOT NULL,        -- Remote peer being synced with
    last_window INTEGER NOT NULL DEFAULT -1,  -- Last window synced (-1 = not started)
    w_param INTEGER NOT NULL DEFAULT 12,      -- Window parameter (number of bits), DEFAULT_W from constants.py
    total_events_seen INTEGER NOT NULL DEFAULT 0,  -- Total events seen (for dynamic w adjustment)
    updated_at INTEGER NOT NULL,     -- Last update timestamp (ms)
    PRIMARY KEY (from_peer_id, to_peer_id)
);

CREATE INDEX IF NOT EXISTS idx_sync_state_from_peer
    ON sync_state(from_peer_id);
