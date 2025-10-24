-- Purge expired events table for forward secrecy
-- Tracks when purges occur for audit trail
-- purge_expired events themselves are permanent (no TTL) to maintain record of deletions
CREATE TABLE IF NOT EXISTS purge_expired_events (
    purge_expired_id TEXT NOT NULL,
    cutoff_ms INTEGER NOT NULL,        -- Absolute timestamp of purge cutoff
    created_at INTEGER NOT NULL,        -- When purge_expired event was created
    recorded_by TEXT NOT NULL,          -- Per-peer recording
    recorded_at INTEGER NOT NULL,       -- When peer recorded it
    PRIMARY KEY (purge_expired_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_purge_expired_by_peer
    ON purge_expired_events(recorded_by, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_purge_expired_by_cutoff
    ON purge_expired_events(cutoff_ms, recorded_by);
