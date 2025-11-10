-- Intro events for hole punching coordination
-- A peer introduces two other peers to each other, facilitating NAT hole punch
-- Example: Alice introduces Bob and Charlie so they can communicate directly

CREATE TABLE IF NOT EXISTS pending_intros (
    intro_id TEXT PRIMARY KEY,
    initiator_peer_id TEXT NOT NULL,  -- Who created this intro (e.g., Alice)
    peer1_id TEXT NOT NULL,  -- First peer to introduce (e.g., Bob)
    peer2_id TEXT NOT NULL,  -- Second peer to introduce (e.g., Charlie)
    created_at INTEGER NOT NULL,  -- When intro was created
    recorded_by TEXT NOT NULL,  -- Which local peer has this event
    recorded_at INTEGER NOT NULL,  -- When event was recorded locally
    processed BOOLEAN DEFAULT FALSE,  -- Whether hole punch was attempted
    UNIQUE (intro_id, recorded_by)
);

-- Index for lookups by recorded_by
CREATE INDEX IF NOT EXISTS idx_pending_intros_lookup
    ON pending_intros(recorded_by, processed);

-- Index for finding intros where peer is involved
CREATE INDEX IF NOT EXISTS idx_pending_intros_peer1
    ON pending_intros(recorded_by, peer1_id, processed);

CREATE INDEX IF NOT EXISTS idx_pending_intros_peer2
    ON pending_intros(recorded_by, peer2_id, processed);
