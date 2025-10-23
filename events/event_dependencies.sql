-- Event dependency tracking for cascading deletion
-- Records parent→child relationships: a child event depends on a parent event
-- Used to ensure convergence when deleting events with dependents

CREATE TABLE IF NOT EXISTS event_dependencies (
    child_event_id TEXT NOT NULL,
    parent_event_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    dependency_type TEXT NOT NULL,  -- 'channel', 'file', 'message', 'key', etc. for debugging
    PRIMARY KEY (child_event_id, parent_event_id, recorded_by)
);

-- Index for efficient parent→children lookup during cascade deletion
CREATE INDEX IF NOT EXISTS idx_event_deps_parent
    ON event_dependencies(parent_event_id, recorded_by);
