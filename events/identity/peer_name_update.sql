-- Peer name updates: encrypted name updates for peers (devices)
-- Each peer has their own view of peer names (subjective table)
-- Stores the latest decrypted peer name and metadata
CREATE TABLE IF NOT EXISTS peer_names (
    peer_id TEXT NOT NULL,
    name TEXT,                      -- decrypted peer name
    encrypted_blob BLOB,            -- encrypted name if not yet decrypted
    event_id TEXT NOT NULL,         -- peer_name_update event ID
    global_count INTEGER NOT NULL,  -- for LWW (last-writer-wins)
    key_id TEXT,                    -- group key used for encryption
    created_at INTEGER NOT NULL,
    signed_by TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (peer_id, recorded_by)
    -- Note: Foreign key constraint omitted due to subjective scoping
);

-- Index for querying peer names (scoped to peer)
CREATE INDEX IF NOT EXISTS idx_peer_names_by_recorded
    ON peer_names(recorded_by, recorded_at DESC);

-- Index for checking if peer has a name
CREATE INDEX IF NOT EXISTS idx_peer_names_by_peer
    ON peer_names(peer_id, recorded_by);
