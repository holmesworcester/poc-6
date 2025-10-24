-- Bootstrap status tracking (subjective)
-- Split into 3 separate tables to avoid INSERT OR REPLACE complexity

-- Tracks peers who created a network
CREATE TABLE IF NOT EXISTS network_creators (
    peer_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (peer_id, recorded_by)
);

-- Tracks peers who joined a network
CREATE TABLE IF NOT EXISTS network_joiners (
    peer_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (peer_id, recorded_by)
);

-- Tracks peers who received first sync response (bootstrap complete)
CREATE TABLE IF NOT EXISTS bootstrap_completers (
    peer_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (peer_id, recorded_by)
);
