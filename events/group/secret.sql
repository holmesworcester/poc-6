-- TreeKEM secrets (local-only symmetric keys for content encryption)
-- Each peer has their own view of which secrets they have access to
CREATE TABLE IF NOT EXISTS secrets (
    secret_id TEXT NOT NULL,
    key BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (secret_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_secrets_recorded_by
ON secrets(recorded_by);
