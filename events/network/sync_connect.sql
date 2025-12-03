-- Connection state table for sync_connect protocol (two-way handshake)
-- Device-wide (no recorded_by) - tracks established connections with remote peers
-- Connections are ephemeral state derived from sync_connect/sync_connect_ack events (LOCAL-ONLY type)

CREATE TABLE IF NOT EXISTS sync_connections (
    peer_shared_id TEXT PRIMARY KEY,        -- Remote peer's public identity
    our_transit_key_id TEXT,                -- Key ID we sent to them (for matching acks)
    their_transit_key_id TEXT,              -- Key ID they provided (for nonce derivation/lookup)
    their_transit_key BLOB,                 -- Symmetric key they provided (to send TO them)
    origin_ip TEXT,                          -- IP address (e.g., "127.0.0.1")
    origin_port INTEGER,                     -- Port number (e.g., 6100)
    last_seen_ms INTEGER NOT NULL,           -- Timestamp of last connect/ack received
    ttl_ms INTEGER NOT NULL DEFAULT 300000   -- Time-to-live in ms (default: 5 minutes)
);

CREATE INDEX IF NOT EXISTS idx_sync_connections_last_seen
ON sync_connections(last_seen_ms);

CREATE INDEX IF NOT EXISTS idx_sync_connections_ttl
ON sync_connections(last_seen_ms, ttl_ms);

CREATE INDEX IF NOT EXISTS idx_sync_connections_our_transit_key
ON sync_connections(our_transit_key_id);
