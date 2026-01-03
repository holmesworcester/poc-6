# Sync Slowness Diagnosis

## Summary

The CLI is slow when syncing images between users. This document captures the root causes and proposes fixes.

## Test Setup

- Two users (Alice, Bob) in a network
- Alice sends a 200KB image (456 slices)
- Measured with 100 ticks of auto-sync

## Key Findings

### Timing Breakdown

| Phase | Time | Per Tick |
|-------|------|----------|
| Initial sync (20 events) | 0.4s | 4.0ms |
| Image sync (480 events) | 8.4s | 84.1ms |

### Per-Job Timing (after image)

| Job | Total Time | Avg Time | Runs |
|-----|------------|----------|------|
| sync_receive | 6.5s | 64.8ms | 100 |
| negentropy_sync | 1.9s | 190.9ms | 10 |
| file_sync | 0.004s | 0.04ms | 100 |
| Others | <0.01s | <1ms | - |

### Root Cause #1: Negentropy Hash Recomputation

When the image is created, it generates 456 `file_slice` events. Each event:
1. Is added to `negentropy_events` table
2. Marks ALL ancestor buckets (8 levels) as stale (hash = NULL)

Result: **677 stale buckets** after image creation.

When `negentropy_sync` runs:
1. Calls `get_root_hash()` to check if sync is needed
2. This triggers recursive `recompute_bucket_hash()` for ALL stale buckets
3. Each bucket recompute involves:
   - LIKE query to find events
   - Sorting event IDs
   - BLAKE2b hash computation
   - Propagating up the tree

**Measured cost:**
- `get_root_hash()` before image: 3.4ms
- `get_root_hash()` after image (first call): **113.8ms**
- `get_root_hash()` cached: 0.01ms

### Root Cause #2: PLACEHOLDER_SYNC = True

With `PLACEHOLDER_SYNC = True`, every sync request triggers sending ALL shareable events:

```python
# From sync.py
if PLACEHOLDER_SYNC:
    # Send ALL shareable events (no filtering)
    shareable_rows = safedb.query("SELECT event_id FROM shareable_events ...")
    events_to_send = [row['event_id'] for row in shareable_rows]
```

This means:
- Alice sends 480 events to Bob on every sync round
- Bob sends 480 events back to Alice
- Each received event marks buckets stale again
- Negentropy never converges because events keep flowing

### Root Cause #3: Dual Sync Mechanisms

Both sync mechanisms are running simultaneously:
1. **PLACEHOLDER_SYNC** - sends all events via `sync.project()`
2. **Negentropy** - does set reconciliation via `negentropy_sync` job

They interfere with each other:
- PLACEHOLDER_SYNC sends events, marking negentropy buckets stale
- Negentropy recomputes hashes, sends its own messages
- Neither converges because the other keeps adding work

## User's Hypotheses Evaluation

| Hypothesis | Verdict |
|------------|---------|
| 1. Too many prekey/autocreated events | **Partially true** - but main issue is file slices (456 per image) |
| 2. Negentropy inefficiency | **TRUE** - O(n) hash recomputation on every sync round |
| 3. Too conservative with autoticks | **FALSE** - autoticks are fine, the jobs are just slow |

## Proposed Fixes

### Quick Fix: Disable One Sync Mechanism

Set `PLACEHOLDER_SYNC = False` OR remove `NegentropySyncJob` from `jobs.JOBS`.

### Better Fix: Lazy Hash Computation

In `sync_all_connections()`, don't compute root hash upfront:

```python
# Current (expensive)
our_root_hash = get_root_hash(db, peer_id)

# Better: Use cached last_synced_root_hash if available
if conn.last_synced_root_hash:
    # Check if we've added events since last sync
    events_since = count_events_since(peer_id, conn.last_sync_time)
    if events_since == 0:
        continue  # Skip - nothing new to sync
```

### Best Fix: Incremental Hash Updates

Instead of marking buckets stale and recomputing on demand:
1. Compute hash delta when adding events
2. Store updated hashes immediately
3. Use bloom filter or XOR-based incremental updates

### Alternative: Batch File Slice Events

Instead of 456 separate events for file slices, use a single manifest event:
```python
# Instead of 456 events
file_slice.create(...) × 456

# Use one event with merkle root
file_manifest.create(
    file_id=...,
    total_slices=456,
    merkle_root=...,
)
```

## Files Modified

- `timing_diagnosis.py` - Basic job timing
- `timing_diagnosis_detailed.py` - Per-tick queue analysis
- `timing_negentropy.py` - Negentropy hash profiling
- `timing_dual_sync.py` - Dual sync mechanism check

## Reproduction

```bash
cd /home/hwilson/poc-6-sync-diagnosis
PYTHONPATH=. /home/hwilson/poc-6/venv/bin/python timing_negentropy.py
```
