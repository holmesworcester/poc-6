-- Schema for address events
CREATE TABLE IF NOT EXISTS addresses (
    peer_id TEXT NOT NULL,
    ip TEXT NOT NULL,
    port INTEGER NOT NULL,
    network_id TEXT NOT NULL,
    registered_at_ms INTEGER NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (peer_id, ip, port)
);

-- Index for looking up active addresses by peer
CREATE INDEX IF NOT EXISTS idx_addresses_peer_active
ON addresses(peer_id, is_active);

-- Index for looking up addresses by network
CREATE INDEX IF NOT EXISTS idx_addresses_network
ON addresses(network_id, is_active);