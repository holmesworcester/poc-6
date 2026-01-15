# Batch Blob Retrieval Optimization Attempt

**Date:** 2024-12-04
**Status:** Abandoned - No significant improvement
**Branch:** poc-6-hash-prefix-buckets (changes not committed)

## Goal

Speed up large file sync by batching database blob retrieval and/or message sending.

## Baseline

50MB file sync (116,509 slices): **68 seconds**

## Approaches Tried

### Approach 1: Batch DB Retrieval + Batch Sending (blob_batch protocol)

Added:
- `db.py`: `get_shareable_blobs(event_ids)` - single SQL query with IN clause
- `connection.py`: `send_batch()` - batches 100 blobs per encrypted envelope
- `sync.py`: `blob_batch` ephemeral event handler
- `negentropy.py`: Updated `_send_event_blobs` to use batching

**Result:** Small files worked, large files (116 blobs+) failed silently. Extensive debugging showed blobs were being sent but not received correctly. Root cause unclear after hours of investigation.

### Approach 2: Batch DB Retrieval + Individual Sends

Simplified approach:
- Keep `get_shareable_blobs()` for batch DB lookup
- Add `connection.send_ids()` that retrieves all blobs in one query, then sends individually
- No protocol changes

**Result:** 50MB sync: **66 seconds** (~3% improvement)

## Analysis

The batch DB retrieval provides minimal improvement because:

1. **DB queries are not the bottleneck.** SQLite queries for individual blobs are fast (~0.1ms each). Even 116K queries add only ~12 seconds.

2. **The real bottleneck is elsewhere:**
   - Encryption overhead (crypto.wrap for each blob)
   - Queue processing (incoming_blobs routing)
   - Projection/validation of received events
   - Network simulation tick overhead

3. **Batch sending (blob_batch) had implementation issues** that were difficult to debug due to the complexity of the routing/encryption stack.

## Recommendations

If sync speed is critical, investigate:

1. **Parallel encryption** - crypto.wrap could potentially be parallelized
2. **Bulk projection** - batch insert/validate instead of per-event
3. **Reduced tick overhead** - the tick() loop adds significant constant time
4. **Protocol-level batching** - send multiple events in a single encrypted frame at the negentropy protocol level, not connection level

The batch retrieval optimization is not worth the added complexity for a 3% improvement.

## Code (Not Committed)

```python
# db.py - SafeDB.get_shareable_blobs()
def get_shareable_blobs(self, event_ids: list[str]) -> dict[str, bytes]:
    """Get multiple blobs from store, only if this peer can share them."""
    if not event_ids:
        return {}
    placeholders = ','.join('?' * len(event_ids))
    results = self._db.query(f"""
        SELECT se.event_id, s.blob
        FROM store s
        INNER JOIN shareable_events se
          ON se.event_id = s.id AND se.can_share_peer_id = ?
        WHERE s.id IN ({placeholders})
    """, (self.recorded_by, *event_ids))
    return {row['event_id']: row['blob'] for row in results}

# connection.py - send_ids()
def send_ids(recorded_by: str, connection_id: str, event_ids: list[str], t_ms: int, db: Any) -> int:
    """Send multiple events by ID, with batch retrieval."""
    if not event_ids:
        return 0
    safedb = create_safe_db(db, recorded_by=recorded_by)
    blobs_by_id = safedb.get_shareable_blobs(event_ids)
    sent = 0
    for event_id in event_ids:
        if event_id in blobs_by_id:
            if send(recorded_by, connection_id, blobs_by_id[event_id], t_ms, db):
                sent += 1
    return sent
```
