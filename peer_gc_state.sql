-- Global counter (Lamport clock) state per peer
-- Tracks highest gc created locally and highest gc seen from peers during sync
CREATE TABLE IF NOT EXISTS peer_gc_state (
    peer_id TEXT PRIMARY KEY,
    highest_gc INTEGER DEFAULT 0
);
