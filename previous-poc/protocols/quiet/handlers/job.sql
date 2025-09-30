-- Job state storage
CREATE TABLE IF NOT EXISTS job_states (
    job_name TEXT PRIMARY KEY,
    state_json TEXT NOT NULL CHECK(json_valid(state_json)),
    updated_ms INTEGER NOT NULL
);

-- Track job runs for scheduling and monitoring
CREATE TABLE IF NOT EXISTS job_runs (
    job_name TEXT PRIMARY KEY,
    last_run_ms INTEGER NOT NULL,
    last_success_ms INTEGER,
    last_failure_ms INTEGER,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    last_state TEXT CHECK(last_state IS NULL OR json_valid(last_state))  -- JSON state at time of last run
);