-- Connection tables for peer-to-peer communication
--
-- Simplified design: connection events contain symmetric keys directly.
-- connection_id is the universal identifier for routing, display, and dependencies.
--
-- Two tables:
-- 1. connections (SUBJECTIVE) - established/pending connections
-- 2. connection_inbox (DEVICE-WIDE) - routing inbox for incoming blobs

-- ============================================================================
-- connections table (SUBJECTIVE)
-- ============================================================================
-- Tracks connections with remote peers, scoped by recorded_by.
-- A connection is PENDING if their_connection_id is NULL (awaiting ack).
-- A connection is ACTIVE if their_connection_id is set (bidirectional).

CREATE TABLE IF NOT EXISTS connections (
    connection_id TEXT NOT NULL,            -- Our connection event ID (mode=req)
    recorded_by TEXT NOT NULL,              -- Local peer who owns this connection

    -- Identity labels (at least one required)
    peer_shared_id TEXT,                    -- Remote peer's public identity (NULL for bootstrap)
    invite_id TEXT,                         -- Invite used for this connection (for bootstrap)

    -- Keys
    our_key BLOB NOT NULL,                  -- Symmetric key we created (they send TO us with this)
    their_connection_id TEXT,               -- Their ack's connection_id (NULL until they ack)
    their_key BLOB,                         -- Symmetric key they created (we send TO them with this)

    -- Lifecycle
    created_at INTEGER NOT NULL,            -- When we sent the request
    last_handshake_ms INTEGER NOT NULL,     -- Updated when req/ack received (NOT on traffic)
    last_traffic_ms INTEGER,                -- STUB: Future - updated when traffic flows on connection
    ttl_ms INTEGER NOT NULL DEFAULT 300000, -- Time-to-live in ms (default: 5 minutes)

    -- Sync optimization
    last_synced_root_hash BLOB,             -- Root hash at last successful sync (skip if unchanged)

    PRIMARY KEY (connection_id, recorded_by),
    CHECK (peer_shared_id IS NOT NULL OR invite_id IS NOT NULL)
);

-- Index for iterating "my connections"
CREATE INDEX IF NOT EXISTS idx_connections_recorded_by
ON connections(recorded_by);

-- Partial indexes for identity lookups (only index rows where identity exists)
CREATE INDEX IF NOT EXISTS idx_connections_peer
ON connections(peer_shared_id, recorded_by)
WHERE peer_shared_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_connections_invite
ON connections(invite_id, recorded_by)
WHERE invite_id IS NOT NULL;

-- Index for expiry cleanup (TTL is based on last_handshake_ms, not traffic)
CREATE INDEX IF NOT EXISTS idx_connections_ttl
ON connections(last_handshake_ms, ttl_ms);


-- ============================================================================
-- connection_inbox table (DEVICE-WIDE - for routing)
-- ============================================================================
-- Device-wide inbox for incoming blobs, routed by connection_id hint.
-- Blobs are stored here before we know which peer they're for.
-- process_inbox() drains this and routes to peer-scoped receive().

CREATE TABLE IF NOT EXISTS connection_inbox (
    id INTEGER PRIMARY KEY,
    connection_id TEXT NOT NULL,            -- Hint from blob (first 16 bytes)
    blob BLOB NOT NULL,                     -- Raw connection-wrapped blob
    received_at INTEGER NOT NULL            -- When blob was received
);

-- Index for routing lookups
CREATE INDEX IF NOT EXISTS idx_connection_inbox_key
ON connection_inbox(connection_id);

-- Index for ordered processing
CREATE INDEX IF NOT EXISTS idx_connection_inbox_received
ON connection_inbox(received_at);
