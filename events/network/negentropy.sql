-- Negentropy-style UNIFIED TIME+HASH sync protocol
--
-- Deterministic sync with finality, requester-driven bisection.
--
-- Key design:
-- - Unified key: (timestamp_ms << 16) | hash_16bits = 64-bit integer
-- - Ranges are arbitrary [start, end) intervals, not fixed buckets
-- - Requester controls bisection: on mismatch, split range in half
-- - Responder is stateless: just compares hashes and sends blobs
-- - Finality: when root hashes match, sync is complete

-- ============================================================================
-- negentropy_events table (SUBJECTIVE - event to unified key mapping)
-- ============================================================================
-- Maps events to their unified key for hash computation.
-- Unified key = (timestamp_ms << 16) | (first 16 bits of event hash)
--
-- This key naturally clusters events by time (high-order bits) while
-- spreading events with same timestamp uniformly (low-order bits).

CREATE TABLE IF NOT EXISTS negentropy_events (
    recorded_by TEXT NOT NULL,
    event_id TEXT NOT NULL,
    unified_key INTEGER NOT NULL,           -- 64-bit: (timestamp_ms << 16) | hash
    created_at INTEGER NOT NULL,            -- Event timestamp (ms since epoch)

    PRIMARY KEY (recorded_by, event_id)
);

-- Index for efficient range queries by unified key
CREATE INDEX IF NOT EXISTS idx_negentropy_events_unified_key
ON negentropy_events(recorded_by, unified_key);

-- ============================================================================
-- negentropy_sync_state table (SUBJECTIVE - per-connection sync state)
-- ============================================================================
-- Tracks ongoing sync with each connection.
-- Each row represents a range [start, end) we're negotiating.
-- Requester bisects ranges on mismatch.

CREATE TABLE IF NOT EXISTS negentropy_sync_state (
    recorded_by TEXT NOT NULL,
    connection_id TEXT NOT NULL,            -- our_transit_key_id from connections
    range_id TEXT NOT NULL,                 -- Unique ID for this range negotiation

    -- Current range we're syncing [start, end)
    range_start INTEGER NOT NULL,           -- Unified key start (inclusive)
    range_end INTEGER NOT NULL,             -- Unified key end (exclusive)

    -- State
    our_hash BLOB,                          -- What we computed for this range
    their_hash BLOB,                        -- What they sent (NULL if we initiated)
    status TEXT NOT NULL DEFAULT 'pending', -- pending|matched|mismatched|complete

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

-- ============================================================================
-- negentropy_chunks table (SUBJECTIVE - precomputed XOR fingerprints)
-- ============================================================================
-- Chunk summaries for O(1) range fingerprint computation.
-- Each chunk covers CHUNK_SIZE (4096) unified_key values.
-- The fingerprint is the XOR of all event fingerprints in that chunk.
-- On insert: XOR new event's fingerprint into the chunk.
-- On delete: XOR event's fingerprint out of the chunk (XOR is self-inverse).

CREATE TABLE IF NOT EXISTS negentropy_chunks (
    recorded_by TEXT NOT NULL,
    chunk_start INTEGER NOT NULL,  -- unified_key boundary (multiple of 4096)
    cnt INTEGER NOT NULL DEFAULT 0,
    fp BLOB NOT NULL,              -- 16-byte XOR fingerprint
    PRIMARY KEY (recorded_by, chunk_start)
);

CREATE INDEX IF NOT EXISTS idx_negentropy_chunks_range
ON negentropy_chunks(recorded_by, chunk_start);

-- ============================================================================
-- negentropy_streaming_state table (SUBJECTIVE - cursor-based sync state)
-- ============================================================================
-- Tracks ongoing streaming for large bulk transfers.
-- When a newcomer has nothing and needs to pull many events, we use
-- cursor-based pagination instead of generating all batch requests at once.

CREATE TABLE IF NOT EXISTS negentropy_streaming_state (
    recorded_by TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    range_id TEXT NOT NULL,
    range_start INTEGER NOT NULL,
    range_end INTEGER NOT NULL,
    cursor INTEGER NOT NULL DEFAULT 0,   -- Last seen unified_key
    items_received INTEGER NOT NULL DEFAULT 0,
    expected_total INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'streaming',  -- streaming|complete|error
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (recorded_by, connection_id, range_id)
);

CREATE INDEX IF NOT EXISTS idx_negentropy_streaming_state_connection
ON negentropy_streaming_state(recorded_by, connection_id, status);
