-- Network settings table - stores network-level configuration
-- Used for star topology sync mode configuration

CREATE TABLE IF NOT EXISTS network_settings (
    network_settings_id TEXT NOT NULL,
    network_id TEXT NOT NULL,
    server_relay_peer_shared_id TEXT,  -- The relay's peer_shared_id (NULL for mesh mode)
    server_relay_address TEXT,  -- Connection info (e.g., 'relay.example.com:5000')
    sync_mode TEXT NOT NULL DEFAULT 'mesh' CHECK (sync_mode IN ('star', 'mesh')),
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (network_settings_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_network_settings_recorded_by ON network_settings(recorded_by);
CREATE INDEX IF NOT EXISTS idx_network_settings_network_id ON network_settings(network_id, recorded_by);
