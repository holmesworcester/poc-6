-- TreeKEM pubkey_shared (shareable tree node public keys for key distribution)
-- Each peer has their view of tree node pubkeys for wrapping secrets
-- Pattern: treekem_pubkey (local) + treekem_pubkey_shared (shareable)
--
-- ATOMICITY: parent_pubkey_shared_id creates a dependency chain from leaf to root.
-- A child pubkey blocks on its parent, ensuring the complete path exists atomically.
CREATE TABLE IF NOT EXISTS treekem_pubkeys_shared (
    treekem_pubkey_shared_id TEXT NOT NULL,
    treekem_pubkey_id TEXT NOT NULL,          -- Reference to local treekem_pubkey event
    depth INTEGER NOT NULL,                   -- Tree depth (0 = root)
    path_prefix BLOB NOT NULL,                -- Path prefix bytes (may be empty for root)
    public_key BLOB NOT NULL,                 -- 32-byte Ed25519 public key
    owner_peer_id TEXT NOT NULL,              -- peer_shared_id who created this pubkey
    parent_pubkey_shared_id TEXT,             -- Parent's treekem_pubkey_shared_id (NULL for root)
    removal_epoch_id TEXT,                    -- Removal epoch for forward secrecy (NULL = initial)
    created_at INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (treekem_pubkey_shared_id, recorded_by)
);

-- Index for looking up pubkeys by tree position
CREATE INDEX IF NOT EXISTS idx_treekem_pubkeys_shared_position
ON treekem_pubkeys_shared(depth, path_prefix, recorded_by, created_at DESC);

-- Index for looking up pubkeys by owner
CREATE INDEX IF NOT EXISTS idx_treekem_pubkeys_shared_owner
ON treekem_pubkeys_shared(owner_peer_id, recorded_by, created_at DESC);

-- Index for looking up pubkeys by removal epoch
CREATE INDEX IF NOT EXISTS idx_treekem_pubkeys_shared_epoch
ON treekem_pubkeys_shared(removal_epoch_id, recorded_by);

-- Index for looking up by treekem_pubkey_id (for key unwrapping)
CREATE INDEX IF NOT EXISTS idx_treekem_pubkeys_shared_pubkey_id
ON treekem_pubkeys_shared(treekem_pubkey_id, recorded_by);
