CREATE TABLE IF NOT EXISTS blocked_events (
    event_id TEXT PRIMARY KEY,
    envelope_json TEXT NOT NULL,  -- Serialized envelope JSON
    missing_deps TEXT NOT NULL    -- JSON array of missing dependency IDs
);

CREATE TABLE IF NOT EXISTS blocked_event_deps (
    event_id TEXT NOT NULL,
    dep_id TEXT NOT NULL,
    PRIMARY KEY (dep_id, event_id),
    FOREIGN KEY (event_id) REFERENCES blocked_events(event_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_blocked_event_deps_event ON blocked_event_deps(event_id);

-- Minimal index of validated events (presence == validated)
CREATE TABLE IF NOT EXISTS validated_events (
    validated_event_id TEXT PRIMARY KEY
);
-- Resolve-deps tables
-- RULE: The resolve_deps handler is only permitted to access the canonical
-- Event Store table `events` and the resolver-owned tables defined here:
--   - validated_events (presence-only validated index)
--   - blocked_events (park envelopes waiting on deps)
--   - blocked_event_deps (dep id tracking for waiters)
-- It MUST NOT query any other projections or application tables.
