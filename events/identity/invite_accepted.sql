-- Invite accepted metadata table (peer-subjective)
-- Stores inviter's connection info from invite link for establishing initial connections

CREATE TABLE IF NOT EXISTS invite_accepteds (
    invite_id TEXT NOT NULL,
    inviter_peer_shared_id TEXT NOT NULL,
    network_id TEXT DEFAULT '',         -- Network ID from invite (for bootstrap)
    user_id TEXT DEFAULT '',            -- User ID from invite (for peer invites / device linking)
    address TEXT,                       -- Inviter IP address (from invite link)
    port INTEGER,                       -- Inviter port number (from invite link)
    inviter_transit_prekey_id TEXT,     -- Inviter's transit prekey ID
    inviter_transit_prekey_public_key BLOB,  -- Inviter's transit prekey
    invite_private_key BLOB,            -- Invite signing key (for sync_connect auth)
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (invite_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_invite_accepteds_recorded_by
ON invite_accepteds(recorded_by);

CREATE INDEX IF NOT EXISTS idx_invite_accepteds_network
ON invite_accepteds(network_id, recorded_by);
