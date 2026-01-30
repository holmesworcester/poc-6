-- Secret broadcast (O(1) sender key distribution via symmetric root secret)
-- When all active peers have converged to the same root secret,
-- sender keys can be distributed with a single symmetric encryption
-- rather than O(log n) asymmetric encryptions to copath nodes.
CREATE TABLE IF NOT EXISTS secrets_broadcast (
    secret_broadcast_id TEXT NOT NULL,
    secret_id TEXT NOT NULL,               -- The sender key being distributed
    source_update_id TEXT,                 -- The treekem_update whose root secret was used (optional)
    created_at INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (secret_broadcast_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_secrets_broadcast_by_peer
    ON secrets_broadcast(recorded_by, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_secrets_broadcast_by_secret
    ON secrets_broadcast(secret_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_secrets_broadcast_by_update
    ON secrets_broadcast(source_update_id, recorded_by);
