# Test Utilities

## tick_helper.py - Sync Convergence Detection

Provides utilities for running sync in tests without hard-coded round counts.

### The Problem

Previously, tests used hard-coded sync loops like:
```python
for i in range(60):
    tick.tick(t_ms=4000 + i*100, db=db)
```

This was fragile - the number of rounds was a guess, and changes to sync behavior could break tests or leave them running longer than necessary.

### The Solution

Use `sync_until_converged()` which automatically detects when sync has stabilized:

```python
from tests.utils import tick_helper

# Instead of guessing round counts, let it run until stable
tick_helper.sync_until_converged(db=db, start_t_ms=None, max_rounds=200, check_interval=1)
```

### How It Works

1. Takes a snapshot of sync state (valid event counts, queue size, blocked counts)
2. Runs ticks and periodically checks for progress
3. Tracks consecutive "no progress" checks
4. After `stability_threshold` (default: 30) consecutive stable checks, declares convergence
5. Returns the round when stability **started** (not when confirmed)

### API

#### sync_until_converged()

```python
def sync_until_converged(
    db: Any,
    start_t_ms: int,
    max_rounds: int = 500,
    check_interval: int = 5,
    verbose: bool = False,
    stability_threshold: int = 30
) -> tuple[int, int, bool, dict]:
```

**Parameters:**
- `db`: Database connection
- `start_t_ms`: Starting timestamp in milliseconds
- `max_rounds`: Maximum sync rounds before giving up (default: 500)
- `check_interval`: Check progress every N rounds (default: 5, use 1 for tests)
- `verbose`: Print progress during sync (default: False)
- `stability_threshold`: Consecutive stable checks before declaring done (default: 30)

**Returns:** `(final_t_ms, rounds_used, converged, status_dict)`
- `final_t_ms`: Timestamp when stability started
- `rounds_used`: Round number when stability started
- `converged`: True if queue was empty when stable (full convergence)
- `status_dict`: Contains `queue_size`, `blocked_count`, `total_valid`

### Example Usage

```python
from tests.utils import tick_helper

def test_my_scenario():
    # ... setup alice, bob, etc ...

    # Initial sync after network creation
    tick_helper.sync_until_converged(db=db, start_t_ms=None, max_rounds=200, check_interval=1)

    # Create some messages
    message.create(...)
    db.commit()

    # Sync messages
    tick_helper.sync_until_converged(db=db, start_t_ms=None, max_rounds=200, check_interval=1)

    # Verify messages synced
    assert ...
```

### Other Utilities

For simpler cases where you know the round count:

```python
# Run fixed number of ticks
tick_helper.run_ticks(db, start_t_ms=None, num_rounds=15)

# Preset helpers
tick_helper.initial_sync(db, start_t_ms=None)  # 15 rounds
tick_helper.message_sync(db, start_t_ms=None)  # 20 rounds
tick_helper.convergence_sync(db, start_t_ms=None)  # 100 rounds
```

### Implementation Details

The convergence detection uses snapshot comparison in `events/network/sync.py`:

- `take_sync_snapshot(db)`: Captures current state (valid counts, queue, blocked)
- `check_sync_progress(db, prev_snapshot)`: Compares against previous snapshot

Progress is detected when:
- Queue size changes (events being processed)
- Blocked count changes (events waiting for dependencies)

When neither changes for `stability_threshold` consecutive checks, sync is considered stable.
