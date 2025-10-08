-- Invite acceptances table: tracks which invites have been accepted by which peers
-- Used for audit and debugging invite flow
CREATE TABLE IF NOT EXISTS invite_acceptances (
    invite_accepted_id TEXT NOT NULL,
    invite_id TEXT NOT NULL,                    -- The invite event that was accepted
    invite_group_key_shared_id TEXT NOT NULL,   -- Expected invite_group_key_shared event
    peer_id TEXT NOT NULL,                      -- Who accepted (local peer_id)
    created_at INTEGER NOT NULL,
    PRIMARY KEY (invite_accepted_id)
);

-- Index for querying acceptances by invite
CREATE INDEX IF NOT EXISTS idx_invite_acceptances_invite
    ON invite_acceptances(invite_id);

-- Index for querying acceptances by peer
CREATE INDEX IF NOT EXISTS idx_invite_acceptances_peer
    ON invite_acceptances(peer_id);
