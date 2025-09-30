-- NetworkGate handler mapping table (transit_secret_id -> network_id, peer_id)
CREATE TABLE IF NOT EXISTS network_gate_transit_index (
    transit_secret_id TEXT PRIMARY KEY,
    peer_id TEXT NOT NULL,
    network_id TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

