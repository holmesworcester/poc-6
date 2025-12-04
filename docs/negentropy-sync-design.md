# Negentropy Sync Design

## Overview

Negentropy is a deterministic sync protocol that uses time-based hierarchical hashes to efficiently identify differences between two peers' event sets. Unlike bloom filters, it has no false positives and provides clear "synced" checkpoints.

## Protocol Flow

1. On connection: send root hash (hash of all year hashes)
2. If root matches: synced! Log checkpoint.
3. If root differs: drill down through time hierarchy
4. At threshold (≤100 events): exchange event IDs
5. Connection layer turns IDs into actual event blobs
6. Include root hash in every message - checkpoint as soon as roots match

## Time Hierarchy

```
root        (hash of all years)
  year      "2024"
    month   "2024-01"
      day   "2024-01-15"
        hour    "2024-01-15-14"
          ten_min "2024-01-15-14-3"
            one_min "2024-01-15-14-35" (leaf)
```

## Hash Computation

**Leaf buckets (1-minute):**
```python
bucket_hash = BLAKE2b-128(concat(sorted(event_ids)))
```

**Parent buckets:**
```python
child_hashes = [(key, hash) for non-empty children]
child_hashes.sort(by=key)
bucket_hash = BLAKE2b-128(concat(key + hash for key, hash in child_hashes))
```

**Root:**
```python
root_hash = BLAKE2b-128(concat(year + hash for year, hash in sorted(year_hashes)))
```

## Threshold Behavior

- **Stub mode**: `EVENTS_THRESHOLD = 1000000` - always send all event IDs immediately
- **Production**: `EVENTS_THRESHOLD = 100` - drill down until bucket has ≤100 events

When bucket has ≤ threshold events, send event IDs instead of drilling further.

## Completion (Checkpoints)

Completion is not final - sets keep changing. A checkpoint is logged whenever:
- We receive a root hash from peer that matches ours
- Root hash is included in every message, so we detect match immediately

```sql
CREATE TABLE negentropy_checkpoints (
    connection_id TEXT,
    recorded_by TEXT,
    completed_at INTEGER,
    root_hash BLOB,
    events_sent INTEGER,
    events_received INTEGER,
    ranges_checked INTEGER
);
```

## API

```python
from events.network import negentropy

# Start sync when connection established
negentropy.sync(db, recorded_by, connection, t_ms)

# Project incoming negentropy event (connection from event)
negentropy.project(db, recorded_by, event, t_ms, get_connection)
```

## CLI Observability

### List connections
```
$ connections
1. ↔ alice (2 mins ago)
2. ↔ bob (15 mins ago)
3. ↔ carol (1 hour ago)
```

### Sync log with tree structure and hash comparison
```
$ sync-log alice

── 10:42:03 ───────────────────────────────────────────
root                  f7a2 vs 3bc1  ↓
  year=2025           9c3f vs 1ab7  ↓
    month=2025-12     c891 vs a034  ↓
      hour=...-04-10  7d2f vs ----  → 4 events
        msg:a1b2c3 msg:d4e5f6 msg:789012 group_key:abcdef
                                     root: abc1 vs abc1 ✓
── checkpoint 10:42:04 | root=abc1 | sent 4, recv 0 ───

── 10:42:47 ───────────────────────────────────────────
root                  abc1 vs abc1  ✓
── checkpoint 10:42:47 | root=abc1 | sent 0, recv 0 ───

── 10:43:12 ───────────────────────────────────────────
root                  d002 vs abc1  ↓  (we created event)
  year=2025           b1e4 vs 9c3f  ↓
    month=2025-12     d002 vs c891  ↓
      day=2025-12-04  f891 vs 5a02  ↓
        hour=...-04-10  8c3a vs 7d2f  ↓
          min=...-10-43 a021 vs ----  → 1 event
            msg:33445566
                                     root: d002 vs d002 ✓
── checkpoint 10:43:14 | root=d002 | sent 1, recv 0 ───

── 10:43:58 ───────────────────────────────────────────
root                  d002 vs e7f3  ↓  (they created events)
  year=2025           b1e4 vs cc02  ↓
    month=2025-12     d002 vs e7f1  ↓
      day=2025-12-04  f891 vs 3ab2  ↓
        hour=...-04-10  8c3a vs 8c3a  ✓
        hour=...-04-11  ---- vs 2f1c  ← 2 events
          msg:fedcba98 user_name:12345678
                                     root: e7f3 vs e7f3 ✓
── checkpoint 10:44:01 | root=e7f3 | sent 0, recv 2 ───
```

### Reading the log

- `a3f2 vs b7c1` - our hash vs their hash
- `↓` - hashes differ, drilling down
- `✓` - hashes match, this range synced
- `→ 4 events` - we sent 4 events (they were missing them)
- `← 2 events` - we received 2 events (we were missing them)
- `----` - that side doesn't have this bucket
- Indentation shows drill-down hierarchy
- Event IDs shown when events are exchanged
- Checkpoint logged whenever root hashes match

### Error cases
```
      hour=...-04-10  7d2f vs 3c1a  ✗ timeout (no response after 5s)
      hour=...-04-11  ---- vs 2f1c  ✗ blob missing for evt:abc123
```

## Integration Points

1. **shareable_events** - source of truth for what events to sync
2. **Connection layer** - turns event IDs into blobs, handles transport
3. **Event projection** - negentropy events are stored and projected like any other

## Progress Tracking

Every message includes `total_events` count, enabling progress display:
```
↔ alice: syncing 45/120 events (37%)
↔ bob: synced ✓
```

## Scenario Test Integration

Replace the existing "sync until convergence" gadget with real sync assertions:

```python
# In scenario tests, instead of:
# tick_until_converged(alice, bob)

# Use checkpoint-based assertions:
def tick_until_synced(scenario, peer1, peer2, connection_id, max_ticks=100):
    """Tick until checkpoint detected or max reached."""
    for _ in range(max_ticks):
        scenario.tick()

        # Check if checkpoint was logged for this connection
        checkpoint = peer1.db.query_one("""
            SELECT * FROM negentropy_checkpoints
            WHERE recorded_by = ? AND connection_id = ?
            ORDER BY completed_at DESC LIMIT 1
        """, (peer1.id, connection_id))

        if checkpoint:
            # Verify both peers have same root hash
            peer1_root = negentropy.get_root_hash(peer1.db, peer1.id)
            peer2_root = negentropy.get_root_hash(peer2.db, peer2.id)
            assert peer1_root == peer2_root, "Checkpoint logged but roots differ"
            return checkpoint

    raise TimeoutError(f"No sync checkpoint after {max_ticks} ticks")
```

This approach:
- Uses real protocol messages, not magic convergence
- Checkpoints are observable completion points
- Can assert specific connection sync states
- Natural integration with existing tick-based scenarios

## Bucket Identification

Buckets are identified by Unix timestamps (ms), not human-readable strings:

```python
# Bucket boundary computation
bucket_start_ms = get_bucket_start_ms(event_timestamp, level)
bucket_end_ms = get_bucket_end_ms(bucket_start_ms, level)

# Example: Year 2024
# start = 1704067200000 (2024-01-01 00:00:00 UTC)
# end = 1735689600000 (2025-01-01 00:00:00 UTC)

# Find children with range queries (no string parsing)
WHERE bucket_start_ms >= ? AND bucket_start_ms < ?
```

Human-readable formatting is only for CLI display:
```python
format_bucket_human(1704067200000, 'year')  # -> "2024"
format_bucket_human(1717200000000, 'month')  # -> "2024-06"
```

## Remaining Work

- [x] Add root level to hierarchy
- [x] Include root hash in every message
- [x] Detect checkpoints (root match)
- [x] Create checkpoints table
- [x] Add total_events for progress tracking
- [x] Use Unix timestamps for bucket identification
- [ ] Wire up to connection abstraction
- [ ] CLI commands: `connections`, `sync-log`
- [ ] Integration with shareable_events
