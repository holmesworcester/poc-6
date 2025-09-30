-- Public prekeys published by peers (event-backed)
CREATE TABLE IF NOT EXISTS prekeys (
    prekey_id TEXT PRIMARY KEY,
    peer_id TEXT NOT NULL,
    network_id TEXT NOT NULL,
    public_key BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_prekeys_peer ON prekeys(peer_id);
CREATE INDEX IF NOT EXISTS idx_prekeys_network ON prekeys(network_id);
CREATE INDEX IF NOT EXISTS idx_prekeys_active ON prekeys(active);

