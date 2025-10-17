-- Addresses table: stores network addresses for peers
-- Subjective table: each peer maintains their own view of peer addresses
CREATE TABLE IF NOT EXISTS addresses (
    address_id TEXT NOT NULL,
    peer_shared_id TEXT NOT NULL,
    ip TEXT NOT NULL,
    port INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (address_id, recorded_by)
);
