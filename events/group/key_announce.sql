-- Key announcements (announces that a key exists for a group)
-- Used to detect missing keys that should be requested
CREATE TABLE IF NOT EXISTS key_announces (
    announce_id TEXT NOT NULL,
    key_id TEXT NOT NULL,              -- The key being announced
    group_id TEXT NOT NULL,            -- Which group this key is for
    removal_epoch_id TEXT,             -- The removal epoch this key requires (NULL = initial/no removals)
    signed_by TEXT NOT NULL,           -- Who announced it
    created_at INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (announce_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_key_announces_by_group
ON key_announces(group_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_key_announces_by_key
ON key_announces(key_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_key_announces_by_epoch
ON key_announces(removal_epoch_id, recorded_by);
