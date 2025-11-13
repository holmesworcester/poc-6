-- Connection state table for sync_connect protocol
-- Device-wide (no recorded_by) - tracks established connections with remote peers
-- Connections are ephemeral state derived from sync_connect events (LOCAL-ONLY type)

CREATE TABLE IF NOT EXISTS sync_connections (
    peer_shared_id TEXT PRIMARY KEY,        -- Remote peer's public identity
    response_transit_key_id TEXT NOT NULL,  -- Key ID hint for sending messages to them
    response_transit_key BLOB NOT NULL,     -- Symmetric key material for encrypting to them
    address TEXT,                            -- IP address (e.g., "127.0.0.1")
    port INTEGER,                            -- Port number (e.g., 6100)
    invite_id TEXT,                          -- Optional: invite used for authentication
    last_seen_ms INTEGER NOT NULL,           -- Timestamp of last connect received
    ttl_ms INTEGER NOT NULL DEFAULT 300000   -- Time-to-live in ms (default: 5 minutes)
);

CREATE INDEX IF NOT EXISTS idx_sync_connections_last_seen
ON sync_connections(last_seen_ms);

CREATE INDEX IF NOT EXISTS idx_sync_connections_ttl
ON sync_connections(last_seen_ms, ttl_ms);
