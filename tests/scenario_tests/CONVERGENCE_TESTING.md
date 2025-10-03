# Convergence Testing Infrastructure

This document describes the convergence failure capture and replay system.

## Overview

The convergence test verifies that events can be projected in any order and still produce the same final state. When failures occur, they are automatically captured for debugging and regression testing.

## What Gets Tested

- **Re-projection**: Can we restore state from the event store?
- **Idempotency**: Can events be projected multiple times without changing state?
- **Convergence**: Do different orderings produce the same final state?

## Automatic Failure Capture

When `assert_convergence()` detects a failure, it automatically:

1. **Saves failure data** to `tests/failures/convergence_failure_<timestamp>_ordering_<N>.json`
2. **Prints details** including baseline order, failed order, and diff
3. **Caps total size** at 50MB to prevent disk bloat

### Failure File Contents

```json
{
  "ordering_number": 42,
  "total_orderings": 5040,
  "baseline_order": ["event1", "event2", "event3", ...],
  "failed_order": ["event3", "event1", "event2", ...],
  "baseline_state": {...},  // Full database state
  "failed_state": {...},    // Full database state
  "difference": "Table 'messages': 2 rows vs 1 rows",
  "timestamp": 1234567890.123,
  "test_name": "convergence"
}
```

## Manual Replay

### Command-line replay:
```bash
python tests/replay_failure.py tests/failures/convergence_failure_1234567890_ordering_42.json
```

### Programmatic replay:
```python
from tests.utils.convergence import replay_from_file

# Replay and verify it matches baseline
replay_from_file(db, 'tests/failures/convergence_failure_1234567890_ordering_42.json')
```

### Custom ordering replay:
```python
from tests.utils.convergence import replay_ordering

# Replay specific event order
final_state = replay_ordering(db, ['event3', 'event1', 'event2'])

# Replay and verify against expected state
final_state = replay_ordering(db, ordering, expected_state=baseline_state)
```

## Regression Testing

Saved failures can become regression tests:

```python
# tests/test_convergence_regressions.py
# Automatically tests all files in tests/failures/
pytest tests/test_convergence_regressions.py
```

**Note**: Currently requires manual test data recreation. Future enhancement will save test setup in failure files.

## Size Management

Failure files can be large (they contain full database snapshots). The system:

- **Caps total size** at 50MB by default
- **Shows size info** when saving: `Saved 2.34MB (total: 15.2MB / 50MB)`
- **Skips saving** if cap would be exceeded
- **Warns you** to delete old failures if needed

To increase the cap:
```python
assert_convergence(db, num_trials=10, save_failures=True)  # Uses default 50MB cap
# Cap is hardcoded in _save_failure(), modify if needed
```

To clear old failures:
```bash
rm -rf tests/failures/
```

## Debugging Tips

### Verbose logging during replay:
```bash
PYTHONPATH=. python tests/replay_failure.py tests/failures/convergence_failure_*.json --log-level=DEBUG
```

### Find patterns in failures:
```bash
# See which events commonly appear in failed orderings
jq '.failed_order' tests/failures/*.json

# Compare differences
jq '.difference' tests/failures/*.json
```

### Test specific orderings:
```python
# In your test code
from tests.utils.convergence import replay_ordering

# Test the "worst case" ordering you discovered
worst_case = ['msg_event', 'channel_event', 'group_event', 'key_event', ...]
state = replay_ordering(db, worst_case)
```

## Test Configuration

### Run fewer permutations (faster):
```python
# Only test 10 random orderings instead of all permutations
assert_convergence(db, num_trials=10)
```

### Stop on first failure:
```python
# Default behavior - stops immediately
assert_convergence(db, stop_on_first_failure=True)

# Test ALL orderings even after failures (slower, finds all issues)
assert_convergence(db, stop_on_first_failure=False)
```

### Disable failure saving:
```python
# Don't save failures to disk
assert_convergence(db, save_failures=False)
```

## Current Test Results

The one-player messaging test currently **PASSES** convergence:
- Tests all 5040 permutations of 7 events
- All orderings produce identical final state
- Takes ~25 seconds to run
- Blocking/unblocking works correctly for encrypted events

The system correctly handles:
- Events arriving before their encryption keys (blocks and unblocks)
- Events arriving before their dependencies (blocks and unblocks)
- All event types (peer, key, group, channel, message)

## Files

- `tests/utils/convergence.py` - Core convergence testing utilities
- `tests/replay_failure.py` - Command-line replay tool
- `tests/test_convergence_regressions.py` - Automated regression tests
- `tests/demo_failure_capture.py` - Demo of the system
- `tests/failures/` - Saved failure files (gitignored)
