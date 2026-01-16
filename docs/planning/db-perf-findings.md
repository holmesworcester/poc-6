# Database Performance Findings

## Summary

**Key insight**: WAL mode's advantage is concurrent access, not single-threaded performance.

- For parallel pytest runs (433 tests, `-n auto`): WAL ≈ memory ≈ 2.7s
- For single-threaded slow tests: WAL is actually slower than no-WAL

## Benchmark Results

### Parallel Test Suite (433 tests, `-n auto`)

| Mode | Time |
|------|------|
| memory | ~2.7s |
| disk + WAL | ~2.7s |
| disk + no WAL | ~10s |

WAL wins because pytest-xdist runs 16 workers, each with its own DB file.
WAL handles concurrent reads/writes efficiently.

### Single-Threaded Message Sync Benchmark

| Messages | memory | wal | no-wal |
|----------|--------|-----|--------|
| 100 | 0.25s | 0.33s | 0.53s |
| 500 | 3.59s | 3.64s | 2.83s |
| 1000 | 12.67s | 13.61s | 14.40s |
| 2000 | 24.82s | **47.85s** | 37.93s |

At 2000 messages, WAL is **1.9x slower** than memory and **1.3x slower** than no-WAL!

### Estimated Times for 10k Messages

| Mode | Estimated Time |
|------|---------------|
| memory | ~2 minutes |
| wal | ~4 minutes |
| no-wal | ~3 minutes |

## Why WAL is Slower for Single-Threaded

1. **WAL overhead**: Every write goes to WAL file first, then checkpoints to main DB
2. **No concurrency benefit**: Single-threaded = no concurrent readers to benefit from WAL
3. **Extra fsync**: WAL mode with `synchronous=NORMAL` still does more I/O than DELETE mode

## Recommendations

### For Parallel Test Suite
Keep `disk + WAL` as default - it's as fast as memory and tests real I/O paths.

### For Slow/Perf Tests
Consider using `mem_db` fixture for slow tests since:
1. They're single-threaded (no WAL benefit)
2. They already take a long time
3. Memory is 2x faster for large operations

### Alternative: Hybrid Approach
```python
@pytest.fixture
def perf_db():
    """Use memory for slow single-threaded perf tests."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)
    return db
```

Or add a marker:
```python
@pytest.mark.slow
@pytest.mark.memory_db  # Override to use memory for this test
def test_sync_perf_10k(mem_db):
    ...
```

## Test Commands

```bash
# Run scaling benchmark
PYTHONPATH=. python3 tests/bench_scaling.py

# Run WAL comparison benchmark
PYTHONPATH=. python3 tests/bench_wal.py

# Run slow tests (currently ~4 min with WAL)
PYTHONPATH=. pytest -m slow -v -n 0
```

## Files

- `tests/bench_scaling.py` - Message sync scaling benchmark
- `tests/bench_wal.py` - WAL vs no-WAL comparison
- `tests/conftest.py` - Fixture definitions (`fresh_db`, `mem_db`, `disk_db`)
