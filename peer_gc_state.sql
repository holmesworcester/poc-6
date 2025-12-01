-- Lamport clock state for unified update pattern
-- Device-wide table tracking the highest global count seen from each peer
-- Used for deterministic convergence in message updates, reactions, and other LWW events
CREATE TABLE IF NOT EXISTS peer_gc_state (
    peer_id TEXT PRIMARY KEY,         -- Peer ID (device-wide, not scoped)
    highest_gc INTEGER NOT NULL       -- Highest global_count seen from this peer
);
