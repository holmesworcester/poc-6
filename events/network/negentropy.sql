-- Negentropy-style time-based sync protocol
--
-- Deterministic sync with finality, designed for UI inspection.
-- Uses time-based hierarchical hashes instead of bloom filters.
--
-- Key design:
-- - Tracks sync state per connection (not per peer)
-- - Range requests narrow down until we reach leaf buckets
-- - Leaf buckets send actual events
-- - Finality: when ranges match, sync is complete
-- - Buckets identified by Unix timestamp (ms), not human-readable strings

-- ============================================================================
-- negentropy_buckets table (SUBJECTIVE - cached hashes per local peer)
-- ============================================================================
-- Hierarchical bucket hashes for efficient sync.
-- When events change, mark ancestors dirty and recompute lazily.

CREATE TABLE IF NOT EXISTS negentropy_buckets (
    recorded_by TEXT NOT NULL,              -- Local peer who owns this state
    level TEXT NOT NULL,                    -- root|year|month|day|hour|ten_min|one_min
    bucket_start_ms INTEGER NOT NULL,       -- Start of bucket period (0 for root)
    hash BLOB,                              -- 16-byte BLAKE2b hash (NULL if needs recompute)
    event_count INTEGER NOT NULL DEFAULT 0, -- Number of events in this bucket (for leaves)
    updated_at INTEGER NOT NULL,

    PRIMARY KEY (recorded_by, level, bucket_start_ms)
);

-- Index for finding children by timestamp range
CREATE INDEX IF NOT EXISTS idx_negentropy_buckets_level
ON negentropy_buckets(recorded_by, level, bucket_start_ms);

-- ============================================================================
-- negentropy_events table (SUBJECTIVE - event to bucket mapping)
-- ============================================================================
-- Maps events to their 1-minute bucket for hash computation.
-- This is the source of truth for what we're syncing.

CREATE TABLE IF NOT EXISTS negentropy_events (
    recorded_by TEXT NOT NULL,
    event_id TEXT NOT NULL,
    bucket_start_ms INTEGER NOT NULL,       -- Start of 1-minute bucket containing this event
    created_at INTEGER NOT NULL,            -- Event timestamp (ms since epoch)

    PRIMARY KEY (recorded_by, event_id)
);

CREATE INDEX IF NOT EXISTS idx_negentropy_events_bucket
ON negentropy_events(recorded_by, bucket_start_ms);

-- ============================================================================
-- negentropy_sync_state table (SUBJECTIVE - per-connection sync state)
-- ============================================================================
-- Tracks ongoing sync with each connection.
-- Each row represents a range we're negotiating.
-- When range_level reaches 'leaf' and hashes match, that range is done.

CREATE TABLE IF NOT EXISTS negentropy_sync_state (
    recorded_by TEXT NOT NULL,
    connection_id TEXT NOT NULL,            -- our_transit_key_id from connections
    range_id TEXT NOT NULL,                 -- Unique ID for this range negotiation

    -- Current range we're syncing
    level TEXT NOT NULL,                    -- root|year|month|day|hour|ten_min|one_min
    bucket_start_ms INTEGER NOT NULL,       -- Start timestamp of bucket we're negotiating

    -- State
    our_hash BLOB,                          -- What we computed for this range
    their_hash BLOB,                        -- What they sent (NULL if we initiated)
    status TEXT NOT NULL DEFAULT 'pending', -- pending|matched|diverged|events_sent|complete

    -- Timing
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,

    PRIMARY KEY (recorded_by, connection_id, range_id)
);

CREATE INDEX IF NOT EXISTS idx_negentropy_sync_state_connection
ON negentropy_sync_state(recorded_by, connection_id, status);

-- ============================================================================
-- negentropy_checkpoints table (SUBJECTIVE - sync completion moments)
-- ============================================================================
-- Logged whenever our root hash matches their root hash.
-- Not "final" since sets keep changing - just a point-in-time observation.

CREATE TABLE IF NOT EXISTS negentropy_checkpoints (
    id INTEGER PRIMARY KEY,
    recorded_by TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    completed_at INTEGER NOT NULL,          -- When root hashes matched
    root_hash BLOB NOT NULL,                -- The matching root hash
    events_sent INTEGER NOT NULL DEFAULT 0,
    events_received INTEGER NOT NULL DEFAULT 0,
    ranges_checked INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_negentropy_checkpoints_connection
ON negentropy_checkpoints(recorded_by, connection_id, completed_at);

