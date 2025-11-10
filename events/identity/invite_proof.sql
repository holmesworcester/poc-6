-- Invite proof table (peer-subjective)
-- Stores proofs that joiners have valid invite private keys
-- Separates invite validation from user/link event structures

CREATE TABLE IF NOT EXISTS invite_proofs (
    invite_proof_id TEXT NOT NULL,
    invite_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('user', 'link')),
    joiner_peer_shared_id TEXT NOT NULL,
    user_id TEXT,            -- for mode='user': the joining user
    link_user_id TEXT,       -- for mode='link': the target user being linked to
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (invite_proof_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_invite_proofs_invite
ON invite_proofs(invite_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_invite_proofs_joiner
ON invite_proofs(joiner_peer_shared_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_invite_proofs_user
ON invite_proofs(user_id, recorded_by);
