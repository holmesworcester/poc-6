-- Connection state tables for peer-to-peer communication
--
-- Two tables with different scoping:
-- 1. connections (SUBJECTIVE) - peer-scoped connection state
-- 2. connection_inbox (DEVICE-WIDE) - routing inbox for incoming blobs

-- ============================================================================
-- connections table (SUBJECTIVE - peer-scoped)
-- ============================================================================
-- Tracks established connections with remote peers, scoped by recorded_by.
-- Connections are keyed by our_transit_key_id (the key we gave them).
-- Identity is labeled by peer_shared_id (when synced) or invite_id (bootstrap).

CREATE TABLE IF NOT EXISTS connections (
    our_transit_key_id TEXT NOT NULL,       -- Key we gave them (they send to us)
    recorded_by TEXT NOT NULL,              -- Local peer who owns this connection

    -- Identity labels (at least one required)
    peer_shared_id TEXT,                    -- Remote peer's public identity (NULL until synced)
    invite_id TEXT,                         -- Invite used for this connection (for bootstrap)

    -- Their key (we send to them)
    their_transit_key_id TEXT,              -- Key ID they provided (for nonce derivation)
    their_transit_key BLOB,                 -- Symmetric key they provided (to send TO them)

    -- Network
    origin_ip TEXT,                         -- IP address (e.g., "127.0.0.1")
    origin_port INTEGER,                    -- Port number (e.g., 6100)

    -- Lifecycle
    last_seen_ms INTEGER NOT NULL,          -- Timestamp of last connect/ack received
    ttl_ms INTEGER NOT NULL DEFAULT 300000, -- Time-to-live in ms (default: 5 minutes)

    PRIMARY KEY (our_transit_key_id, recorded_by),
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

-- Index for expiry cleanup
CREATE INDEX IF NOT EXISTS idx_connections_ttl
ON connections(last_seen_ms, ttl_ms);


-- ============================================================================
-- connection_inbox table (DEVICE-WIDE - for routing)
-- ============================================================================
-- Device-wide inbox for incoming blobs, routed by transit key hint.
-- Blobs are stored here before we know which peer they're for.
-- process_inbox() drains this and routes to peer-scoped receive().

CREATE TABLE IF NOT EXISTS connection_inbox (
    id INTEGER PRIMARY KEY,
    our_transit_key_id TEXT NOT NULL,       -- Hint from blob (first 16 bytes)
    blob BLOB NOT NULL,                     -- Raw transit-wrapped blob
    received_at INTEGER NOT NULL            -- When blob was received
);

-- Index for routing lookups
CREATE INDEX IF NOT EXISTS idx_connection_inbox_key
ON connection_inbox(our_transit_key_id);

-- Index for ordered processing
CREATE INDEX IF NOT EXISTS idx_connection_inbox_received
ON connection_inbox(received_at);
