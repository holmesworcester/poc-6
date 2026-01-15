# Negentropy Sync Design

## Overview

Negentropy is a deterministic sync protocol that uses hierarchical hashes to efficiently identify differences between two peers' event sets. Unlike bloom filters, it has no false positives and provides clear "synced" checkpoints.

## Protocol Flow

1. On connection: send root hash (hash of all child bucket hashes)
2. If root matches: synced! Log checkpoint.
3. If root differs: drill down through unified key hierarchy
4. At threshold (≤100 events): exchange event IDs
5. Connection layer turns IDs into actual event blobs
6. Include root hash in every message - checkpoint as soon as roots match

## Unified Key Design

The bucket key is a unified value combining timestamp and event hash:

```
unified_key = timestamp_hex (12 chars, 48 bits) + event_hash (4 chars, 16 bits)
            = 16 hex chars total (64 bits)
```

This design provides:
- **Temporal locality**: Events cluster by time (high-order bits are timestamp)
- **Uniform distribution within time**: Same-timestamp events spread by hash (low-order bits)
- **Large file support**: 1GB file = 2.4M slices at same timestamp distributed across 65K buckets

## Bucket Hierarchy

```
root              (hash of all prefix_2 buckets)
  prefix_2        (first 2 hex chars = 8 bits)
    prefix_4      (first 4 hex chars = 16 bits)
      prefix_6    (first 6 hex chars = 24 bits)
        prefix_8  (first 8 hex chars = 32 bits)
          prefix_10  (first 10 hex chars = 40 bits)
            prefix_12  (first 12 hex chars = 48 bits = full timestamp)
              prefix_14  (first 14 hex chars = timestamp + 8 bits of hash)
                prefix_16  (first 16 hex chars = full unified key, leaf)
```

**Key insight**: Levels prefix_2 through prefix_12 use only timestamp bits. Levels prefix_14 and prefix_16 use the hash suffix, enabling same-timestamp events (like file slices) to be split across different buckets.

### Bucket Distribution Example (1GB File)

A 1GB file creates ~2.4 million file_slice events, all with the same timestamp:

| Level | Buckets | Events per bucket |
|-------|---------|-------------------|
| prefix_12 | 1 | 2.4M (all same timestamp) |
| prefix_14 | 256 | ~9,300 |
| prefix_16 | 65,536 | ~36 ✓ |

## Hash Computation

**Leaf buckets (prefix_16):**
```python
bucket_hash = BLAKE2b-128(concat(sorted(event_ids)))
```

**Parent buckets:**
```python
child_hashes = [(prefix, hash) for non-empty children]
child_hashes.sort(by=prefix)
bucket_hash = BLAKE2b-128(concat(prefix + hash for prefix, hash in child_hashes))
```

## Threshold Behavior

When a bucket has more than `EVENTS_THRESHOLD` (100) events and we're not at the finest level (prefix_16), we drill down to child buckets. When bucket has ≤100 events OR we're at prefix_16, send event IDs directly.

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

# Add event to sync buckets (called by recorded.py and file_slice.py)
negentropy.add_event_to_sync(db, recorded_by, event_id, timestamp)

# Compute bucket hash
hash = negentropy.compute_bucket_hash(db, recorded_by, prefix, level)

# Handle incoming sync message
responses = negentropy.handle_incoming(db, recorded_by, connection_id, message, t_ms)

# Start/continue sync on connection
negentropy.sync_all_connections(db, recorded_by, t_ms)
```

## Integration Points

1. **recorded.py** - Calls `add_event_to_sync()` when projecting shareable events
2. **file_slice.py** - Calls `add_event_to_sync()` when creating file slices
3. **Connection layer** - Turns event IDs into blobs, handles transport
4. **sync.py** - Routes negentropy messages to handler

## Tables

### negentropy_events
Maps events to their unified key for bucket membership:
```sql
CREATE TABLE negentropy_events (
    recorded_by TEXT NOT NULL,
    event_id TEXT NOT NULL,
    unified_key TEXT NOT NULL,  -- 16-char hex: timestamp (12) + hash (4)
    created_at INTEGER NOT NULL,
    PRIMARY KEY (recorded_by, event_id)
);
```

### negentropy_buckets
Cached bucket hashes (recomputed lazily when dirty):
```sql
CREATE TABLE negentropy_buckets (
    recorded_by TEXT NOT NULL,
    level TEXT NOT NULL,        -- root|prefix_2|...|prefix_16
    prefix TEXT NOT NULL,       -- unified key prefix
    hash BLOB,                  -- NULL if needs recompute
    event_count INTEGER,
    updated_at INTEGER,
    PRIMARY KEY (recorded_by, level, prefix)
);
```

### negentropy_sync_state
Per-connection sync state:
```sql
CREATE TABLE negentropy_sync_state (
    recorded_by TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    range_id TEXT NOT NULL,
    level TEXT NOT NULL,
    prefix TEXT NOT NULL,
    our_hash BLOB,
    their_hash BLOB,
    status TEXT NOT NULL,       -- pending|matched|diverged|events_sent|complete
    PRIMARY KEY (recorded_by, connection_id, range_id)
);
```

## CLI Observability

### Sync status
```
$ sync-status
Connection alice: synced ✓ (root: a3f2b1c4)
Connection bob: syncing... 3 ranges pending
```

### Reading the sync log

- `a3f2 vs b7c1` - our hash vs their hash
- `↓` - hashes differ, drilling down
- `✓` - hashes match, this range synced
- `→ 4 events` - we sent 4 events (they were missing them)
- `← 2 events` - we received 2 events (we were missing them)
- `----` - that side doesn't have this bucket
