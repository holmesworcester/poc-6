# Tick and Jobs Refactor Plan

## Problem Statement

Currently, `tick.py` and `jobs.py` have duplicate information and limited functionality:

1. **Duplication**: `tick.py` hardcodes all job calls, while `jobs.py` lists them in a registry that isn't used
2. **No Frequency Control**: All jobs run every tick (potentially every few seconds), wasting CPU/battery
3. **No State Tracking**: Jobs can't remember when they last ran or maintain state between runs
4. **No Prioritization**: Can't mark jobs as critical vs. optional
5. **No Observability**: Can't see job execution history or performance metrics

## Goal

Make `tick.py` pull from `jobs.py` and run jobs based on their configuration, with each job eventually tracking its own state and determining when it should run.

## Design Principles

1. **Single Source of Truth**: `jobs.py` is the registry, `tick.py` is the executor
2. **Incremental Migration**: Start simple, add features progressively
3. **Backward Compatible**: Existing tests should continue to work
4. **Stateful Jobs**: Jobs maintain their own execution history and state
5. **Pluggable**: Easy to add new jobs without modifying `tick.py`

## Implementation Phases

### Phase 1: Basic Integration (Immediate)

**Goal**: Make `tick.py` execute jobs from `jobs.py` registry

**Changes**:
```python
# tick.py
from typing import Any
import jobs

def tick(t_ms: int, db: Any) -> None:
    """Run all periodic jobs from the jobs registry."""
    for job in jobs.JOBS:
        fn = job['fn']
        params = job.get('params', {})

        # Call with t_ms and db as required args
        fn(t_ms=t_ms, db=db, **params)
        db.commit()
```

**Benefits**:
- No code duplication
- Easy to add/remove jobs
- Jobs listed in one place

**Risks**:
- Still runs every job every tick (no optimization yet)

---

### Phase 2: Basic Frequency Control (Next Sprint)

**Goal**: Only run jobs when their interval has elapsed

**Changes**:
```python
# jobs.py
JOBS = [
    {
        'name': 'sync_send',
        'fn': sync.send_request_to_all,
        'every_ms': 5_000,  # Now enforced!
        'params': {},
    },
    # ...
]

# tick.py
import jobs

# Global state (in-memory for now)
_last_run_times = {}

def tick(t_ms: int, db: Any) -> None:
    """Run periodic jobs based on their frequency."""
    for job in jobs.JOBS:
        job_name = job['name']
        every_ms = job['every_ms']

        # Check if enough time has passed
        last_run = _last_run_times.get(job_name, 0)
        if t_ms - last_run >= every_ms:
            fn = job['fn']
            params = job.get('params', {})

            fn(t_ms=t_ms, db=db, **params)
            db.commit()

            _last_run_times[job_name] = t_ms
```

**Benefits**:
- Huge performance improvement
- Respects job frequency requirements
- Battery-friendly for mobile

**Limitations**:
- State is lost on restart (in-memory only)
- No persistence

---

### Phase 3: Persistent Job State (Future)

**Goal**: Jobs persist their state to the database

**Schema**:
```sql
-- New table for job execution state
CREATE TABLE IF NOT EXISTS job_state (
    job_name TEXT PRIMARY KEY,
    last_run_at INTEGER NOT NULL,  -- Timestamp in ms
    last_duration_ms INTEGER,       -- How long last run took
    last_status TEXT,               -- 'success', 'error', 'skipped'
    last_error TEXT,                -- Error message if failed
    total_runs INTEGER DEFAULT 0,
    total_errors INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
```

**Changes**:
```python
# tick.py
def tick(t_ms: int, db: Any) -> None:
    """Run periodic jobs based on their frequency and state."""
    unsafedb = create_unsafe_db(db)

    for job in jobs.JOBS:
        job_name = job['name']
        every_ms = job['every_ms']

        # Load state from database
        state = unsafedb.query_one(
            "SELECT last_run_at FROM job_state WHERE job_name = ?",
            (job_name,)
        )
        last_run = state['last_run_at'] if state else 0

        # Check if should run
        if t_ms - last_run >= every_ms:
            start_time = time.time()
            try:
                fn = job['fn']
                params = job.get('params', {})
                result = fn(t_ms=t_ms, db=db, **params)

                duration_ms = int((time.time() - start_time) * 1000)

                # Update state
                unsafedb.execute(
                    """INSERT OR REPLACE INTO job_state
                       (job_name, last_run_at, last_duration_ms, last_status,
                        total_runs, total_errors, updated_at)
                       VALUES (?, ?, ?, 'success',
                               COALESCE((SELECT total_runs FROM job_state WHERE job_name = ?), 0) + 1,
                               COALESCE((SELECT total_errors FROM job_state WHERE job_name = ?), 0),
                               ?)""",
                    (job_name, t_ms, duration_ms, job_name, job_name, t_ms)
                )
                db.commit()

            except Exception as e:
                # Record error
                duration_ms = int((time.time() - start_time) * 1000)
                unsafedb.execute(
                    """INSERT OR REPLACE INTO job_state
                       (job_name, last_run_at, last_duration_ms, last_status, last_error,
                        total_runs, total_errors, updated_at)
                       VALUES (?, ?, ?, 'error', ?,
                               COALESCE((SELECT total_runs FROM job_state WHERE job_name = ?), 0) + 1,
                               COALESCE((SELECT total_errors FROM job_state WHERE job_name = ?), 0) + 1,
                               ?)""",
                    (job_name, t_ms, duration_ms, str(e), job_name, job_name, t_ms)
                )
                db.commit()
                log.error(f"Job {job_name} failed: {e}")
```

**Benefits**:
- State survives restarts
- Can query job execution history
- Enables monitoring and alerting
- Can implement backoff strategies on errors

---

### Phase 4: Self-Scheduling Jobs (Future)

**Goal**: Jobs decide when they should run based on their own logic

**Design**:
```python
# jobs.py
class Job:
    """Base class for self-scheduling jobs."""

    def __init__(self, name: str, every_ms: int):
        self.name = name
        self.every_ms = every_ms

    def should_run(self, t_ms: int, db: Any) -> bool:
        """Determine if this job should run now.

        Default implementation uses simple frequency check,
        but subclasses can override with custom logic.
        """
        unsafedb = create_unsafe_db(db)
        state = unsafedb.query_one(
            "SELECT last_run_at FROM job_state WHERE job_name = ?",
            (self.name,)
        )
        last_run = state['last_run_at'] if state else 0
        return t_ms - last_run >= self.every_ms

    def run(self, t_ms: int, db: Any) -> dict:
        """Execute the job. Must be implemented by subclass."""
        raise NotImplementedError


class PrekeyReplenishmentJob(Job):
    """Replenishes prekeys only when count is low."""

    def __init__(self):
        super().__init__('prekey_replenishment', every_ms=3_600_000)  # 1 hour

    def should_run(self, t_ms: int, db: Any) -> bool:
        """Run if frequency elapsed AND prekeys are low."""
        if not super().should_run(t_ms, db):
            return False

        # Additional check: only run if prekeys actually low
        unsafedb = create_unsafe_db(db)
        peers = unsafedb.query("SELECT peer_id FROM local_peers")

        for peer in peers:
            count = unsafedb.query_one(
                "SELECT COUNT(*) as c FROM transit_prekeys WHERE owner_peer_id = ? AND ttl_ms > ?",
                (peer['peer_id'], t_ms)
            )
            if count['c'] < MIN_TRANSIT_PREKEYS:
                return True  # At least one peer needs replenishment

        return False  # All peers have enough prekeys

    def run(self, t_ms: int, db: Any) -> dict:
        return transit_prekey.replenish_for_all_peers(t_ms, db)


# Registry of job instances
JOBS = [
    PrekeyReplenishmentJob(),
    SyncSendJob(),
    PurgeExpiredJob(),
    # ...
]
```

**tick.py**:
```python
def tick(t_ms: int, db: Any) -> None:
    """Run jobs that determine they should run."""
    for job in jobs.JOBS:
        if job.should_run(t_ms, db):
            try:
                result = job.run(t_ms, db)
                # Record success...
            except Exception as e:
                # Record error...
```

**Benefits**:
- Jobs encapsulate their own scheduling logic
- Easy to implement complex scheduling (backoff, conditional execution)
- Better separation of concerns
- Testable in isolation

---

### Phase 5: Priority and Parallel Execution (Far Future)

**Goal**: Run critical jobs first, parallelize where safe

**Design**:
```python
# jobs.py
class Job:
    priority: int = 5  # 1 (highest) to 10 (lowest)
    can_run_parallel: bool = False

# tick.py
def tick(t_ms: int, db: Any) -> None:
    """Run jobs in priority order, parallelizing where safe."""

    # Filter to jobs that should run
    runnable_jobs = [j for j in jobs.JOBS if j.should_run(t_ms, db)]

    # Sort by priority
    runnable_jobs.sort(key=lambda j: j.priority)

    # Separate parallel-safe from sequential
    sequential_jobs = [j for j in runnable_jobs if not j.can_run_parallel]
    parallel_jobs = [j for j in runnable_jobs if j.can_run_parallel]

    # Run sequential jobs first (in priority order)
    for job in sequential_jobs:
        job.run(t_ms, db)
        db.commit()

    # Run parallel jobs concurrently (if any)
    if parallel_jobs:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(job.run, t_ms, db) for job in parallel_jobs]
            for future in futures:
                future.result()
                db.commit()
```

**Benefits**:
- Critical jobs never starved
- Better resource utilization
- Faster overall execution

---

## Migration Strategy

### Step 1: Phase 1 Implementation (This Week)
- Refactor `tick.py` to call jobs from `jobs.py`
- Ensure all existing tests pass
- Document new pattern for adding jobs

### Step 2: Phase 2 Implementation (Next Sprint)
- Add in-memory frequency tracking
- Update tests to account for frequency control
- Measure performance improvement

### Step 3: Schema Migration (Future)
- Add `job_state` table to schema
- Migrate to persistent state
- Add observability queries

### Step 4: Class-Based Jobs (Future)
- Refactor jobs to use Job base class
- Implement should_run() for smart scheduling
- Add job-specific tests

### Step 5: Advanced Features (As Needed)
- Priority scheduling
- Parallel execution
- Distributed job execution (multi-device)

## Testing Strategy

1. **Unit Tests**: Each job class has its own tests
2. **Integration Tests**: Test tick() with various job configurations
3. **Performance Tests**: Measure CPU usage, job execution time
4. **Scenario Tests**: Existing forward secrecy tests should continue to pass

## Success Metrics

- **Code Duplication**: Zero (tick.py reads from jobs.py)
- **CPU Usage**: 50-90% reduction (frequency control)
- **Battery Impact**: Measurable improvement on mobile
- **Maintainability**: New jobs added in <10 lines
- **Observability**: Can query job history and metrics

## Open Questions

1. **Should job state be per-peer or device-wide?**
   - Proposal: Device-wide for now, per-peer if needed

2. **How to handle job failures?**
   - Proposal: Exponential backoff + alerting

3. **Should jobs be event-sourced?**
   - Proposal: Not initially, but schema allows it

4. **How to handle job dependencies?**
   - Proposal: Phase 6+ feature, use DAG scheduler

5. **Should we support async jobs?**
   - Proposal: Yes, but Phase 5+

## References

- Current `tick.py`: `/home/hwilson/poc-6-forward-secrecy/tick.py`
- Current `jobs.py`: `/home/hwilson/poc-6-forward-secrecy/jobs.py`
- Celery (inspiration): https://docs.celeryq.dev/
- APScheduler (inspiration): https://apscheduler.readthedocs.io/
