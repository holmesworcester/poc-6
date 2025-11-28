# Batch Projection Design

## Current Architecture

```
for event in pending_events:
    input_dict = resolve(event_type, event_id, ...)  # DB reads
    result = projector.project(input_dict)            # Pure (no DB)
    apply_result(result, ...)                         # DB writes
```

Each event:
- 1+ blob lookups (event + dependencies)
- 1+ dependency queries
- 1+ INSERT statements

## Proposed Batch Architecture

```
# Phase 1: Batch resolve
input_dicts = batch_resolve(events)  # Single pass: bulk blob fetch + bulk dep queries

# Phase 2: Batch project (embarrassingly parallel)
results = [projector.project(input) for input in input_dicts]

# Phase 3: Batch apply (single transaction)
batch_apply(results)  # One transaction, bulk INSERTs
```

## Phase 1: Batch Resolution

### Current resolve() does:
1. `store.get(event_id)` - fetch blob
2. `crypto.unwrap()` - decrypt if needed
3. `crypto.verify_event()` - verify signature
4. For each dependency: query DB or fetch another blob

### Batched version:
```python
def batch_resolve(events: list[tuple[str, str, str, int]]) -> list[dict]:
    """Resolve multiple events in a single pass.

    events: list of (event_type, event_id, recorded_by, recorded_at)
    """
    # 1. Bulk fetch all blobs
    event_ids = [e[1] for e in events]
    blobs = store.get_many(event_ids, db)  # NEW: batch blob fetch

    # 2. Decrypt/parse all (CPU-bound, could parallelize)
    parsed = [unwrap_and_parse(blob, recorded_by) for blob, recorded_by in ...]

    # 3. Collect all dependency IDs needed
    all_dep_ids = set()
    for event_type, event_data in parsed:
        spec = get_spec(event_type)
        for dep_spec in spec.get("dependencies", []):
            dep_id = extract_dep_id(dep_spec, event_data)
            if dep_id:
                all_dep_ids.add(dep_id)

    # 4. Bulk fetch dependency blobs
    dep_blobs = store.get_many(all_dep_ids, db)

    # 5. Bulk query dependency tables
    # Example: batch linked_peer lookups
    peer_ids = [...]
    linked_peers = batch_query_linked_peers(peer_ids, recorded_by, db)

    # 6. Assemble input dicts
    return [build_input_dict(...) for ...]
```

### New store.get_many():
```python
def get_many(event_ids: list[str], db) -> dict[str, bytes]:
    """Fetch multiple blobs in one query."""
    placeholders = ','.join(['?' for _ in event_ids])
    rows = db.execute(
        f"SELECT event_id, data FROM events WHERE event_id IN ({placeholders})",
        tuple(event_ids)
    ).fetchall()
    return {row['event_id']: row['data'] for row in rows}
```

## Phase 2: Batch Projection

Already perfect for batching - project() is pure:
```python
results = [projector.project(input) for input in input_dicts]

# Or parallel (if CPU-bound crypto is significant):
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor() as executor:
    results = list(executor.map(projector.project, input_dicts))
```

## Phase 3: Batch Apply

### Current apply_result():
```python
for table_name, rows in result.tables.items():
    for row in rows:
        sql = f"INSERT OR IGNORE INTO {table_name} (...) VALUES (...)"
        db.execute(sql, values)
```

### Batched version:
```python
def batch_apply(results: list[ProjectorResult], recorded_by: str, db) -> int:
    """Apply multiple results in a single transaction.

    Returns: number of rows written
    """
    # Collect all rows by table
    by_table: dict[str, list[dict]] = defaultdict(list)
    for result in results:
        if result.valid and not result.blocked:
            for table_name, rows in result.tables.items():
                by_table[table_name].extend(rows)

    # Single transaction
    with db.transaction():
        total = 0
        for table_name, rows in by_table.items():
            total += bulk_insert(table_name, rows, db)
        return total

def bulk_insert(table_name: str, rows: list[dict], db) -> int:
    """Insert many rows efficiently."""
    if not rows:
        return 0

    # All rows should have same columns
    columns = list(rows[0].keys())
    placeholders = ','.join(['?' for _ in columns])
    column_list = ','.join(columns)

    # Option A: executemany (SQLite optimizes this)
    sql = f"INSERT OR IGNORE INTO {table_name} ({column_list}) VALUES ({placeholders})"
    values = [tuple(row[c] for c in columns) for row in rows]
    db.executemany(sql, values)

    # Option B: Single INSERT with multiple value sets (even faster for large batches)
    # INSERT OR IGNORE INTO messages (...) VALUES (...), (...), (...)

    return len(rows)
```

## Atomicity Options

### Option 1: All-or-nothing batch
```python
with db.transaction():
    batch_apply(results)
# If any INSERT fails, whole batch rolls back
```

### Option 2: Per-event atomicity within batch
```python
for result in results:
    try:
        with db.transaction():
            apply_result(result)
    except Exception:
        # Log failure, continue with others
        pass
```

### Option 3: Savepoints (partial rollback)
```python
with db.transaction():
    for result in results:
        savepoint = db.savepoint()
        try:
            apply_result(result)
        except Exception:
            db.rollback_to(savepoint)
```

### Recommendation: All-or-nothing for sync batches

For sync, events come in batches from a peer. If we successfully receive them,
we should project them atomically. Partial projection = inconsistent state.

```python
def project_sync_batch(events: list, recorded_by: str, db) -> int:
    """Project a batch of events received during sync.

    All-or-nothing: either all events project or none do.
    This matches sync semantics - we received the batch atomically.
    """
    input_dicts = batch_resolve(events, recorded_by, db)
    results = [project(input) for input in input_dicts]

    # Filter to valid results
    valid_results = [r for r in results if r.valid and not r.blocked]

    with db.transaction():
        return batch_apply(valid_results, recorded_by, db)
```

## Handling Blocked Events

Blocked events (missing dependencies) need special handling:

```python
def project_batch_with_retry(events, recorded_by, db):
    """Project batch, retrying blocked events after dependencies resolve."""

    input_dicts = batch_resolve(events, recorded_by, db)
    results = [project(input) for input in input_dicts]

    valid = []
    blocked = []

    for event, result in zip(events, results):
        if result.blocked:
            blocked.append((event, result.missing_deps))
        elif result.valid:
            valid.append(result)

    # Apply valid results
    with db.transaction():
        batch_apply(valid, recorded_by, db)

    # Retry blocked after this batch (dependencies may now exist)
    if blocked:
        retry_events = [e for e, _ in blocked]
        # Queue for retry or immediate re-resolve
        return project_batch_with_retry(retry_events, recorded_by, db)
```

## Performance Estimates

Current (1000 messages):
- 1000 blob fetches (1000 queries)
- 1000 INSERT statements
- ~1000 transactions (if auto-commit)

Batched (1000 messages):
- 1 bulk blob fetch (1 query with IN clause)
- 1 executemany INSERT (1 statement, SQLite batches internally)
- 1 transaction

Expected speedup: **10-100x** for batch projection
(SQLite is fastest when you minimize round-trips and use transactions)

## API Design

```python
# projectors/__init__.py additions

def batch_resolve(
    events: list[tuple[str, str, str, int]],  # (type, id, recorded_by, recorded_at)
    db: Any
) -> list[dict | None]:
    """Resolve multiple events efficiently."""
    ...

def batch_apply(
    results: list[ProjectorResult],
    recorded_by: str,
    recorded_at: int,
    db: Any
) -> int:
    """Apply multiple results in one transaction. Returns rows written."""
    ...

def project_batch(
    events: list[tuple[str, str, str, int]],
    db: Any
) -> tuple[int, list[tuple[str, list[str]]]]:
    """Project a batch of events.

    Returns: (rows_written, blocked_events)
        blocked_events: list of (event_id, missing_deps)
    """
    input_dicts = batch_resolve(events, db)

    results = []
    blocked = []

    for (event_type, event_id, recorded_by, recorded_at), input_dict in zip(events, input_dicts):
        if input_dict is None:
            continue
        module = _PROJECTORS[event_type]
        result = module.project(input_dict)
        if result.blocked:
            blocked.append((event_id, result.missing_deps))
        else:
            results.append((result, recorded_by, recorded_at))

    # Apply all valid results atomically
    rows = batch_apply([r for r, _, _ in results], ...)

    return rows, blocked
```

## Integration with Sync

```python
# sync.py - receive path

def receive_events(peer_id: str, events: list[bytes], t_ms: int, db):
    """Receive and project a batch of events from sync."""

    # Store all blobs first
    event_ids = []
    for blob in events:
        event_id = store.put(blob, db)  # Could also batch this
        event_ids.append(event_id)

    # Batch project
    events_to_project = [
        (detect_type(blob), event_id, peer_id, t_ms)
        for blob, event_id in zip(events, event_ids)
    ]

    rows, blocked = project_batch(events_to_project, db)

    log.info(f"Projected {rows} rows from {len(events)} events, {len(blocked)} blocked")

    return rows, blocked
```

## Next Steps

1. **Implement store.get_many()** - bulk blob fetch
2. **Implement batch_resolve()** - bulk resolution
3. **Implement batch_apply()** - bulk INSERT with executemany
4. **Add batch_query helpers** - for dependency lookups
5. **Integrate with sync receive path** - use batch projection
6. **Benchmark** - measure actual speedup

## Resolved Questions (from codebase research)

### 1. Batch Size Limits

**SQLite limit: 999 parameters per query**

| Use Case | Recommended Chunk Size | Reasoning |
|----------|------------------------|-----------|
| IN clause (1 column) | 500-700 items | 500 params, leaves buffer |
| VALUES (2 columns) | 400-450 rows | 450 × 2 = 900 params |
| VALUES (3+ columns) | 250-300 rows | 300 × 3 = 900 params |
| Bulk INSERT | 300-400 rows | Same parameter counting |

**Existing pattern** (queues.py:210-220):
```python
placeholders = ','.join(['(?, ?)' for _ in items])
params = []
for item in items:
    params.extend([item['id'], item['recorded_by']])
db.execute(f"DELETE ... WHERE (id, recorded_by) IN (VALUES {placeholders})", tuple(params))
```

**Safe chunking helper**:
```python
CHUNK_SIZE = 450  # Safe for 2-column VALUES

def chunked(items, size=CHUNK_SIZE):
    for i in range(0, len(items), size):
        yield items[i:i + size]
```

### 2. Memory Pressure

**YES, memory is a real concern.**

- **input_dict size**: 500 bytes - 2 KB each (baseline + content)
- **Current batch size**: 2000 blobs per sync.receive()
- **Memory per batch**: ~1-4 MB for input_dicts alone
- **Evidence**: MAX_CANDIDATES=2000 limit exists specifically for memory, with TODO noting it breaks test_sync_perf_10k

**Mitigation strategies already in codebase**:
1. `batch_mode` flag suppresses logging during bulk ops (store.py)
2. LIMIT clauses bound all result sets
3. Kahn's algorithm avoids materializing full dependency graphs
4. Per-peer scoping prevents cross-peer memory aliasing

**Recommendation**: Process in chunks of 500-1000, not full 2000 batch at once.

### 3. Blocked Event Retry Strategy

**Current system uses reactive unblocking (Kahn's algorithm)**:

```python
# When event becomes valid:
unblocked_ids = queues.blocked.notify_event_valid(event_id, recorded_by, safedb)
if unblocked_ids:
    recorded.project_ids(unblocked_ids, db)  # Re-project immediately
```

**Tables**:
- `blocked_events_ephemeral` - tracks blocked events with `deps_remaining` counter
- `blocked_event_deps_ephemeral` - tracks which deps each event needs

**For batch projection, use immediate retry within batch**:
```python
def project_batch_with_retry(events, recorded_by, db, max_passes=3):
    """Retry blocked events after each pass (deps may now exist)."""
    for pass_num in range(max_passes):
        input_dicts = batch_resolve(events, recorded_by, db)
        results = [project(d) for d in input_dicts if d]

        valid = [(e, r) for e, r in zip(events, results) if r.valid and not r.blocked]
        blocked = [(e, r) for e, r in zip(events, results) if r.blocked]

        # Apply valid results
        with db.transaction():
            for event, result in valid:
                apply_result(result, ...)
                # Notify for reactive unblocking of OTHER events
                queues.blocked.notify_event_valid(event[1], event[2], safedb)

        if not blocked:
            return  # All done

        events = [e for e, _ in blocked]

    # Remaining events go to blocked queue for later
    for event, result in blocked:
        queues.blocked.add(event[1], event[2], result.missing_deps, safedb)
```

**Key insight**: Don't reinvent - integrate with existing `queues.blocked` system.

### 4. Error Handling

**Current pattern: silent failure for bad data, blocking for missing resources**

| Error Type | Detection | Handling | Recovery |
|------------|-----------|----------|----------|
| Missing blob | `store.get()` returns `b''` | Return None | Skip, no retry |
| JSON parse error | Exception in `parse_json()` | Catch, log | Skip, no retry |
| Decrypt failure | Exception in decrypt/unseal | Return (None, []) | Skip, no retry |
| Missing key | Key not in DB | Return (None, [key_id]) | Block, retry when key arrives |
| Missing dependency | Not in valid_events | check_deps() | Block, retry when dep arrives |
| Invalid signature | verify_event() → False | Return None | Skip, no retry (permanent) |

**For batch projection**:
```python
def batch_resolve_safe(events, recorded_by, db):
    """Resolve with error isolation - one bad event doesn't kill batch."""
    results = []
    for event_type, event_id, recorded_by, recorded_at in events:
        try:
            input_dict = resolve(event_type, event_id, recorded_by, recorded_at, db)
            results.append(input_dict)
        except Exception as e:
            log.error(f"Failed to resolve {event_id[:20]}: {e}")
            results.append(None)  # Skip this event
    return results
```

**Error summary for batch operations**:
```python
@dataclass
class BatchResult:
    rows_written: int = 0
    successful: int = 0
    blocked: list[tuple[str, list[str]]] = field(default_factory=list)  # (event_id, missing_deps)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (event_id, reason)
```

**Philosophy**: Bad data (corrupt, invalid signature) is permanently rejected. Missing resources (keys, deps) are retried. This matches sync semantics - we don't want to keep retrying fundamentally broken events.
