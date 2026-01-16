-- Trust anchors: network_ids that should be validated when they arrive
-- invite_accepted stores network_id here (not in valid_events)
-- When network event arrives, resolver checks this table to allow validation
CREATE TABLE IF NOT EXISTS trust_anchors (
    network_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    created_at INTEGER,
    PRIMARY KEY (network_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_trust_anchors_recorded_by ON trust_anchors(recorded_by);
