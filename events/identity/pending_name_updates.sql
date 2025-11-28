-- Pending name updates: stores intent to create name events when key is not available yet
-- Used during join flow when username/network_name/peer_name needs to be encrypted but key hasn't arrived
-- Deterministic trigger in group_key_shared projector retries creation when key becomes available
CREATE TABLE IF NOT EXISTS pending_name_updates (
    id TEXT NOT NULL,
    type TEXT NOT NULL,  -- 'username', 'network_name', 'peer_name'
    entity_id TEXT NOT NULL,  -- user_id, network_id, or peer_id that this name updates
    name TEXT NOT NULL,  -- plaintext name to encrypt and create event for
    peer_id TEXT NOT NULL,  -- peer that initiated this update
    peer_shared_id TEXT NOT NULL,  -- peer_shared_id that will sign the event
    status TEXT NOT NULL DEFAULT 'waiting_for_key',  -- 'waiting_for_key', 'created', 'failed'
    error TEXT,  -- error message if status='failed'
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (id, recorded_by)
);

-- Index for finding pending items by status
CREATE INDEX IF NOT EXISTS idx_pending_name_updates_status
    ON pending_name_updates(status, recorded_by);

-- Index for finding pending items by type and entity
CREATE INDEX IF NOT EXISTS idx_pending_name_updates_entity
    ON pending_name_updates(type, entity_id, recorded_by);

-- Pending name decrypts: stores encrypted name blobs when we can't decrypt them yet
-- Used when we receive a username_update/network_name_update/peer_name_update event
-- but don't have the group key needed for decryption
-- Deterministic trigger in group_key_shared projector retries decryption when key arrives
CREATE TABLE IF NOT EXISTS pending_name_decrypts (
    id TEXT NOT NULL,
    type TEXT NOT NULL,  -- 'username', 'network_name', 'peer_name'
    entity_id TEXT NOT NULL,  -- user_id, network_id, or peer_id that this name is for
    event_id TEXT NOT NULL,  -- the name_update event we couldn't decrypt
    encrypted_blob BLOB NOT NULL,  -- the encrypted name bytes
    key_id TEXT NOT NULL,  -- which group key is needed for decryption
    status TEXT NOT NULL DEFAULT 'waiting_for_key',  -- 'waiting_for_key', 'decrypted', 'failed'
    error TEXT,  -- error message if status='failed'
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (id, recorded_by)
);

-- Index for finding pending decrypts by status
CREATE INDEX IF NOT EXISTS idx_pending_name_decrypts_status
    ON pending_name_decrypts(status, recorded_by);

-- Index for finding pending decrypts by entity
CREATE INDEX IF NOT EXISTS idx_pending_name_decrypts_entity
    ON pending_name_decrypts(type, entity_id, recorded_by);
