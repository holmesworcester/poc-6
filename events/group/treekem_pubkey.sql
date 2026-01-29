-- TreeKEM Phase 2: treekem_pubkeys (tree node public keys)
-- Each peer has their own view of tree node pubkeys for key wrapping
CREATE TABLE IF NOT EXISTS treekem_pubkeys (
    treekem_pubkey_id TEXT NOT NULL,
    depth INTEGER NOT NULL,              -- Tree depth (0 = root)
    path_prefix BLOB NOT NULL,           -- Path prefix bytes (may be empty for root)
    public_key BLOB NOT NULL,            -- 32-byte Ed25519 public key
    owner_peer_id TEXT NOT NULL,         -- peer_shared_id who created this pubkey
    parent_pubkey_id TEXT,               -- Parent pubkey in tree (NULL for root)
    removal_epoch_id TEXT,               -- Removal epoch for forward secrecy (NULL = initial)
    created_at INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (treekem_pubkey_id, recorded_by)
);

-- Index for looking up pubkeys by tree position
CREATE INDEX IF NOT EXISTS idx_treekem_pubkeys_position
ON treekem_pubkeys(depth, path_prefix, recorded_by, created_at DESC);

-- Index for looking up pubkeys by owner
CREATE INDEX IF NOT EXISTS idx_treekem_pubkeys_owner
ON treekem_pubkeys(owner_peer_id, recorded_by, created_at DESC);

-- Index for looking up pubkeys by removal epoch
CREATE INDEX IF NOT EXISTS idx_treekem_pubkeys_epoch
ON treekem_pubkeys(removal_epoch_id, recorded_by);

-- Local-only secret storage for treekem_pubkey private keys
-- Only the owner has access to these (not synced)
CREATE TABLE IF NOT EXISTS treekem_pubkey_secrets (
    treekem_pubkey_id TEXT NOT NULL,
    private_key BLOB NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (treekem_pubkey_id, recorded_by)
);
