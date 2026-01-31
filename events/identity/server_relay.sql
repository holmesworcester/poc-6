-- Server relay table - stores server relay peer information
-- Server relays have peer identity but NO user identity
-- They can sync events but cannot decrypt messages or take admin actions

CREATE TABLE IF NOT EXISTS server_relays (
    server_relay_id TEXT NOT NULL,
    peer_shared_id TEXT NOT NULL,
    network_id TEXT NOT NULL,
    address TEXT,  -- Optional connection address (e.g., 'relay.example.com:5000')
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (peer_shared_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_server_relays_network ON server_relays(network_id, recorded_by);
CREATE INDEX IF NOT EXISTS idx_server_relays_recorded_by ON server_relays(recorded_by);
