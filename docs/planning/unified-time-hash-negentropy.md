# Unified Time+Hash Negentropy Protocol

## Overview

Replace prefix-based bucketing with interval-based bisection. Ranges are arbitrary `[start, end)` intervals in a unified time+hash keyspace. On mismatch, the **requester** bisects and sends child requests.

## Unified Key Format

```
unified_key = (timestamp_ms << 16) | (hash_16bits)
            = 64-bit integer
```

- High 48 bits: timestamp in milliseconds
- Low 16 bits: first 16 bits of event_id hash (for tie-breaking same-ms events)

**Ordering**: Time-major, hash-minor. Bisecting naturally clusters by time first.

## Protocol Messages

### range_request
```python
{
    'type': 'range_request',
    'range_id': str,
    'start': int,      # inclusive, 64-bit unified key
    'end': int,        # exclusive, 64-bit unified key
    'hash': str,       # hex of XOR fingerprint for this range
    'root_hash': str,  # for checkpoint detection
    'total_events': int,
}
```

### range_matched
```python
{
    'type': 'range_matched',
    'range_id': str,
    'root_hash': str,
    'total_events': int,
}
```

### range_mismatched
```python
{
    'type': 'range_mismatched',
    'range_id': str,
    'event_count': int,  # helps requester decide if worth splitting further
    'root_hash': str,
    'total_events': int,
}
```

**Removed**: `range_events` - blobs are sent directly, no separate message type needed.

## Protocol Flow

```
Requester                              Responder
    |                                      |
    |-- range_request(0, MAX, H_req) ----->|
    |                                      | compute H_resp for [0, MAX)
    |                                      |
    |<-------- range_mismatched -----------| (hashes differ, many events)
    |                                      |
    | bisect: mid = MAX/2                  |
    |                                      |
    |-- range_request(0, mid, H1) -------->|
    |-- range_request(mid, MAX, H2) ------>|
    |                                      |
    |<-------- range_matched (H1) ---------| (left half synced)
    |<-------- range_mismatched -----------| (right half differs)
    |                                      |
    | bisect right half...                 |
    |                                      |
    ... continue until ranges small enough ...
    |                                      |
    |<-------- [blobs sent directly] ------| (≤ threshold events)
    |<-------- range_matched --------------| (after blobs, hashes match)
```

## Responder Logic

```python
def handle_range_request(start, end, their_hash):
    our_hash = compute_range_hash(start, end)

    if our_hash == their_hash:
        return range_matched()

    event_count = count_events_in_range(start, end)

    if event_count <= THRESHOLD:
        # Send blobs directly
        events = get_events_in_range(start, end)
        send_blobs(events)
        return range_matched()  # or empty - requester retries
    else:
        return range_mismatched(event_count=event_count)
```

## Requester Logic

```python
def handle_range_mismatched(range_id, event_count):
    start, end = get_pending_range(range_id)

    if event_count <= THRESHOLD:
        # Responder should have sent blobs, wait for retry
        return

    # Bisect
    mid = (start + end) // 2

    send_range_request(start, mid)
    send_range_request(mid, end)
```

## Hash Computation

XOR fingerprinting over events in range:

```python
def compute_range_hash(start: int, end: int) -> bytes:
    """Compute XOR of fingerprints for all events in [start, end)."""
    result = bytes(16)  # zero

    for event in get_events_where(start <= unified_key < end):
        fingerprint = compute_fingerprint(event.id)
        result = xor_bytes(result, fingerprint)

    return result
```

**Optimization** (future): Maintain sorted events with prefix-XOR for O(log n) range queries.

## Database Schema Changes

### negentropy_events (modified)
```sql
CREATE TABLE negentropy_events (
    recorded_by TEXT NOT NULL,
    event_id TEXT NOT NULL,
    unified_key INTEGER NOT NULL,  -- 64-bit: (timestamp_ms << 16) | hash
    created_at INTEGER NOT NULL,

    PRIMARY KEY (recorded_by, event_id)
);

CREATE INDEX idx_negentropy_events_key
ON negentropy_events(recorded_by, unified_key);
```

### negentropy_sync_state (modified)
```sql
CREATE TABLE negentropy_sync_state (
    recorded_by TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    range_id TEXT NOT NULL,

    range_start INTEGER NOT NULL,  -- unified key start (inclusive)
    range_end INTEGER NOT NULL,    -- unified key end (exclusive)

    our_hash BLOB,
    their_hash BLOB,
    status TEXT NOT NULL,  -- pending|matched|mismatched|complete

    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,

    PRIMARY KEY (recorded_by, connection_id, range_id)
);
```

### Remove: negentropy_buckets
No longer needed - no precomputed bucket hashes.

## Implementation Steps

1. **Update unified_key computation**
   - Change `compute_unified_key(event_id, created_at)` to return 64-bit int
   - Format: `(created_at << 16) | (hash_16bits)`

2. **Update database schema**
   - Migrate `negentropy_events.unified_key` from TEXT to INTEGER
   - Update `negentropy_sync_state` to use `range_start/range_end`
   - Drop `negentropy_buckets` table

3. **Update protocol handlers**
   - `init_sync_for_connection`: send range_request(0, MAX)
   - `handle_range_request`: compute hash for arbitrary range
   - `handle_range_mismatched`: bisect and send child requests (NEW)
   - Remove prefix/level logic

4. **Update wire format**
   - Add start/end to range_request encoding
   - Remove level/prefix from encoding
   - Add range_mismatched message type

5. **Fix failing tests**
   - Update test expectations for new message types
   - Remove tests for prefix-based behavior

## Constants

```python
MAX_UNIFIED_KEY = (1 << 64) - 1  # 0xFFFFFFFFFFFFFFFF
EVENTS_THRESHOLD = 50  # send blobs when ≤ 50 events in range
```

## Benefits

1. **No predetermined structure** - ranges are dynamic
2. **Temporal locality** - recent events cluster in keyspace
3. **Requester-controlled** - responder is stateless
4. **Simple bisection** - same logic for all levels
5. **Handles large files** - same-timestamp events spread by hash suffix
