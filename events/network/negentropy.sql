-- Negentropy-style UNIFIED TIME+HASH sync protocol
--
-- Deterministic sync with finality, designed for UI inspection.
-- Uses a unified key: timestamp || event_hash for bucketing.
--
-- Key design:
-- - Tracks sync state per connection (not per peer)
-- - Range requests narrow down by unified key prefix
-- - Unified key = timestamp_hex (12 chars) + event_hash (4 chars)
-- - Events cluster by time but spread uniformly within same timestamp
-- - Finality: when ranges match, sync is complete
-- - Buckets identified by prefix of unified key

-- ============================================================================
-- negentropy_buckets table (SUBJECTIVE - cached hashes per local peer)
-- ============================================================================
-- Hierarchical bucket hashes for efficient sync.
-- When events change, mark ancestors dirty and recompute lazily.

CREATE TABLE IF NOT EXISTS negentropy_buckets (
    recorded_by TEXT NOT NULL,              -- Local peer who owns this state
    level TEXT NOT NULL,                    -- root|prefix_2|prefix_4|prefix_6|prefix_8
    prefix TEXT NOT NULL DEFAULT '',        -- Unified key prefix ('' for root)
    hash BLOB,                              -- 16-byte BLAKE2b hash (NULL if needs recompute)
    event_count INTEGER NOT NULL DEFAULT 0, -- Number of events in this bucket (for leaves)
    updated_at INTEGER NOT NULL,

    PRIMARY KEY (recorded_by, level, prefix)
);

-- Index for finding children by prefix
CREATE INDEX IF NOT EXISTS idx_negentropy_buckets_prefix
ON negentropy_buckets(recorded_by, level, prefix);

-- ============================================================================
-- negentropy_events table (SUBJECTIVE - event to bucket mapping)
-- ============================================================================
-- Maps events to their unified key for hash computation.
-- This is the source of truth for what we're syncing.
--
-- unified_key format: timestamp_hex (12 chars, 48 bits ms) + event_hash (4 chars, 16 bits)
-- Example: "01932a4b5c6d" + "a1b2" = "01932a4b5c6da1b2"
--
-- This key naturally clusters events by time (high-order bits) while
-- spreading events with same timestamp uniformly (low-order bits).

CREATE TABLE IF NOT EXISTS negentropy_events (
    recorded_by TEXT NOT NULL,
    event_id TEXT NOT NULL,
    unified_key TEXT NOT NULL,              -- 16-char hex: timestamp (12) + hash (4)
    created_at INTEGER NOT NULL,            -- Event timestamp (ms since epoch)

    PRIMARY KEY (recorded_by, event_id)
);

-- Index for efficient bucket lookups by unified key prefix
CREATE INDEX IF NOT EXISTS idx_negentropy_events_unified_key
ON negentropy_events(recorded_by, unified_key);

-- ============================================================================
-- negentropy_sync_state table (SUBJECTIVE - per-connection sync state)
-- ============================================================================
-- Tracks ongoing sync with each connection.
-- Each row represents a range we're negotiating.
-- When range hashes match, that range is done.

CREATE TABLE IF NOT EXISTS negentropy_sync_state (
    recorded_by TEXT NOT NULL,
    connection_id TEXT NOT NULL,            -- our_transit_key_id from connections
    range_id TEXT NOT NULL,                 -- Unique ID for this range negotiation

    -- Current range we're syncing
    level TEXT NOT NULL,                    -- root|prefix_2|prefix_4|prefix_6|prefix_8
    prefix TEXT NOT NULL DEFAULT '',        -- Unified key prefix we're negotiating

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
    recorded_by TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    completed_at INTEGER NOT NULL,          -- When root hashes matched
    root_hash BLOB NOT NULL,                -- The matching root hash
    events_sent INTEGER NOT NULL DEFAULT 0,
    events_received INTEGER NOT NULL DEFAULT 0,
    ranges_checked INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (recorded_by, connection_id, completed_at)
);

CREATE INDEX IF NOT EXISTS idx_negentropy_checkpoints_connection
ON negentropy_checkpoints(connection_id, completed_at);
