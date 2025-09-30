-- Minimal canonical Event Store
CREATE TABLE IF NOT EXISTS events (
    event_id   TEXT PRIMARY KEY,
    event_blob BLOB NOT NULL,
    visibility TEXT CHECK(visibility IN ('local-only','network')) DEFAULT 'local-only'
);

CREATE INDEX IF NOT EXISTS idx_events_visibility ON events(visibility);

-- Projected events table (for event type specific projections)
CREATE TABLE IF NOT EXISTS projected_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    projection_data TEXT NOT NULL,  -- JSON of projected data
    projected_at INTEGER NOT NULL,
    FOREIGN KEY(event_id) REFERENCES events(event_id)
);

-- Visibility tracking: which identity has ingested/seen which event
CREATE TABLE IF NOT EXISTS seen_events (
    identity_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    seen_at INTEGER NOT NULL,
    PRIMARY KEY(identity_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_seen_events_event ON seen_events(event_id);
