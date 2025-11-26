CREATE TABLE IF NOT EXISTS networks (
    network_id TEXT NOT NULL,
    creator_user_id TEXT NOT NULL DEFAULT '',
    network_pubkey TEXT NOT NULL DEFAULT '',  -- Phase 4: Network's own public key (base64)
    signed_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (network_id, recorded_by)
);
