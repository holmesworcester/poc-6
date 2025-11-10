-- Job state table for tracking recurring job execution
-- Device-wide table (not scoped by peer)
CREATE TABLE IF NOT EXISTS job_state (
    job_name TEXT PRIMARY KEY,
    last_run_at INTEGER NOT NULL,  -- Timestamp in ms when job last ran
    updated_at INTEGER NOT NULL     -- Timestamp in ms when record last updated
);
