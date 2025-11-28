-- Network name updates: encrypted name updates for networks
-- Each peer has their own view of network names (subjective table)
-- Stores the latest decrypted network name and metadata
CREATE TABLE IF NOT EXISTS network_names (
    network_id TEXT NOT NULL,
    name TEXT,                      -- decrypted network name
    encrypted_blob BLOB,            -- encrypted name if not yet decrypted
    event_id TEXT NOT NULL,         -- network_name_update event ID
    global_count INTEGER NOT NULL,  -- for LWW (last-writer-wins)
    key_id TEXT,                    -- group key used for encryption
    created_at INTEGER NOT NULL,
    signed_by TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (network_id, recorded_by)
    -- Note: Foreign key constraint omitted due to subjective scoping
    -- (networks table has different recorded_by scope)
);

-- Index for querying network names (scoped to peer)
CREATE INDEX IF NOT EXISTS idx_network_names_by_recorded
    ON network_names(recorded_by, recorded_at DESC);

-- Index for checking if network has a name
CREATE INDEX IF NOT EXISTS idx_network_names_by_network
    ON network_names(network_id, recorded_by);
