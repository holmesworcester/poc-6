-- Network address observations (peer-announced)
-- Peers announce what endpoints they observe for other peers
-- This allows Byzantine fault tolerance: multiple peers can attest to same address

CREATE TABLE IF NOT EXISTS network_addresses (
    address_id TEXT PRIMARY KEY,
    observed_peer_id TEXT NOT NULL,  -- Which peer was observed
    observed_by_peer_id TEXT NOT NULL,  -- Which peer made the observation
    ip TEXT NOT NULL,  -- Observed IP address
    port INTEGER NOT NULL,  -- Observed port
    created_at INTEGER NOT NULL,  -- When observation was made
    recorded_by TEXT NOT NULL,  -- Which local peer has this event
    recorded_at INTEGER NOT NULL,  -- When this event was recorded locally
    UNIQUE (address_id, recorded_by)
);

-- Index for lookups by recorded_by and observed_peer_id
CREATE INDEX IF NOT EXISTS idx_network_addresses_lookup
    ON network_addresses(recorded_by, observed_peer_id);

-- Index for lookups by who made observation
CREATE INDEX IF NOT EXISTS idx_network_addresses_observed_by
    ON network_addresses(recorded_by, observed_by_peer_id);
