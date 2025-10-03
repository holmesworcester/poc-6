-- Invite key shared events table: tracks group keys wrapped to invite prekeys
-- Used during invite creation to share the group key with future invitees
-- Note: Multiple peers can decrypt the same invite_key_shared event (if they use the same invite link)
CREATE TABLE IF NOT EXISTS invite_keys_shared (
    invite_key_shared_id TEXT NOT NULL,
    original_key_id TEXT NOT NULL,         -- The key being shared (group key)
    created_by TEXT NOT NULL,              -- Inviter's peer_shared_id
    created_at INTEGER NOT NULL,
    recipient_invite_prekey_id TEXT NOT NULL, -- Invite prekey this was wrapped to
    recorded_by TEXT NOT NULL,         -- Who saw this event
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (invite_key_shared_id, recorded_by)
);

-- Index for querying invite keys by recipient
CREATE INDEX IF NOT EXISTS idx_invite_keys_shared_recipient
    ON invite_keys_shared(recipient_invite_prekey_id);

-- Index for querying by original key
CREATE INDEX IF NOT EXISTS idx_invite_keys_shared_key
    ON invite_keys_shared(original_key_id);
