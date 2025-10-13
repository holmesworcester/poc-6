-- Group prekeys for sealing group keys to members (subjective)
-- Each peer's view of their own group prekeys within network contexts
CREATE TABLE IF NOT EXISTS group_prekeys (
    prekey_id TEXT NOT NULL,
    owner_peer_id TEXT NOT NULL,  -- Which local peer owns this
    public_key BLOB NOT NULL,
    private_key BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,    -- Subjective view
    PRIMARY KEY (prekey_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_group_prekeys_owner
ON group_prekeys(owner_peer_id, recorded_by);
