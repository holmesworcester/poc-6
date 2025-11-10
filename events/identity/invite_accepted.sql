-- Invite accepted metadata table (peer-subjective)
-- Stores inviter's connection info from invite link for establishing initial connections

CREATE TABLE IF NOT EXISTS invite_accepteds (
    invite_id TEXT NOT NULL,
    inviter_peer_shared_id TEXT NOT NULL,
    address TEXT,                       -- Inviter IP address (from invite link)
    port INTEGER,                       -- Inviter port number (from invite link)
    inviter_transit_prekey_id TEXT,     -- Inviter's transit prekey ID
    inviter_transit_prekey_public_key BLOB,  -- Inviter's transit prekey
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (invite_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_invite_accepteds_recorded_by
ON invite_accepteds(recorded_by);
