-- Connection state table for sync_connect protocol (two-way handshake)
-- Keyed by our_transit_key_id (the key we gave them - they use it to talk to us)
-- Connections are ephemeral state derived from sync_connect/sync_connect_ack events (LOCAL-ONLY type)

CREATE TABLE IF NOT EXISTS sync_connections (
    our_transit_key_id TEXT PRIMARY KEY,     -- Key ID we gave them (they use this to talk to us)
    our_peer_id TEXT NOT NULL,               -- Which of our peers this connection is for (routing)
    their_transit_key BLOB,                  -- Symmetric key they gave us (to send TO them)
    their_transit_key_id TEXT,               -- Their key ID (for nonce derivation)
    last_seen_ms INTEGER NOT NULL,           -- Timestamp of last connect/ack received
    ttl_ms INTEGER NOT NULL DEFAULT 300000   -- Time-to-live in ms (default: 5 minutes)
);

CREATE INDEX IF NOT EXISTS idx_sync_connections_our_peer
ON sync_connections(our_peer_id);

CREATE INDEX IF NOT EXISTS idx_sync_connections_last_seen
ON sync_connections(last_seen_ms);
