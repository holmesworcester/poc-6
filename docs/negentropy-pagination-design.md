# Negentropy Protocol: Pagination Without range_events

## Problem

The current negentropy sync protocol has a throughput bottleneck: `EVENT_ID_MAX = 15` limits the number of event IDs that can be sent in a single wire message. With 50ms RTT, syncing 1,000 messages takes ~27 rounds (~370 msgs/s).

The `range_events` message was designed for bidirectional sync - Alice sends her event IDs, Bob compares and sends back his event IDs. But with job ordering ensuring blobs are indexed before requests are processed (SyncUpdateJob before SyncRespondJob), this bidirectional exchange is no longer necessary.

## Solution: Leaf-Level Pagination

Remove `range_events` entirely. At leaf level, send three things:

1. **ALL event blobs** for the current leaf (no 15-event cap)
2. **range_request for current leaf** (verification/checksum)
3. **range_request for next leaf** (pagination)

### Why This Works

**Job ordering guarantees**: SyncUpdateJob runs before SyncRespondJob, so:
- Blobs arrive and get indexed first
- Then range_requests are processed against the now-updated index

**Current leaf range_request acts as verification**:
- Recipient computes hash after indexing received blobs
- If hashes match → `range_matched` (done with this leaf)
- If hashes differ → `range_request` back (packet loss, triggers resend)

**Next leaf range_request enables pipelining**:
- No extra round trip needed to move to next leaf
- Recipient processes next leaf immediately (may send more blobs)

### Protocol Flow

```
Alice                                    Bob
  |                                        |
  |--- range_request(root, hash=X) ------->|
  |                                        | Bob: hash differs, jump to first leaf
  |<-- [blobs for leaf 00]                 |
  |<-- range_request(leaf 00, hash=Y)      | (verification)
  |<-- range_request(leaf 01, hash=Z) -----|  (pagination)
  |                                        |
  | SyncUpdateJob indexes blobs            |
  | SyncRespondJob processes requests      |
  |                                        |
  | Leaf 00: hash matches                  |
  |--- range_matched(leaf 00) ------------>|
  |                                        |
  | Leaf 01: we have events too            |
  |--- [blobs for leaf 01]                 |
  |--- range_request(leaf 01, hash=W)      |
  |--- range_request(leaf 02, hash=...) -->|
  |                                        |
  ... continues until all leaves synced ...
```

### Jump to Leaf Optimization

When their hash is empty (null/zero) but we have events:
- Don't drill down hierarchically through prefix_2 → prefix_4 → prefix_6
- Jump directly to first leaf and start sending blobs + pagination

This reduces rounds for the common case of syncing to an empty peer.

## Implementation Changes

### 1. Remove range_events

```python
# DELETE: MSG_RANGE_EVENTS = 3
# DELETE: handle_range_events() function
# DELETE: routing in handle_sync_message()
```

### 2. Modify handle_range_request() Leaf Behavior

```python
# At leaf level (level == 'prefix_6' or event_count <= threshold):

# Send ALL blobs (remove EVENT_ID_MAX cap)
event_ids = get_events_in_bucket(db, recorded_by, prefix, level)
_send_event_blobs(db, recorded_by, connection_id, event_ids, t_ms)

# Verification: range_request for current leaf
responses.append({
    'type': 'range_request',
    'range_id': generate_range_id(),
    'level': level,
    'prefix': prefix,
    'hash': our_hash.hex(),
    ...
})

# Pagination: range_request for next leaf
next_prefix = compute_next_prefix(prefix, level)
if next_prefix:
    next_hash = recompute_bucket_hash(db, recorded_by, level, next_prefix)
    if next_hash:  # Only if we have events
        responses.append({
            'type': 'range_request',
            ...
        })
```

### 3. Add compute_next_prefix() Helper

```python
def compute_next_prefix(prefix: str, level: str) -> str | None:
    """Compute the next sibling prefix for pagination.

    E.g., prefix="00" at prefix_2 → "01"
          prefix="ff" at prefix_2 → None (no more siblings)
    """
    prefix_len = LEVEL_PREFIX_LEN[level]
    if not prefix:
        return None  # root has no siblings

    prefix_int = int(prefix, 16)
    next_int = prefix_int + 1
    max_val = (1 << (prefix_len * 4)) - 1

    if next_int > max_val:
        return None

    return f"{next_int:0{prefix_len}x}"
```

## Expected Results

| Metric | Before | After |
|--------|--------|-------|
| 1k messages rounds | 27 | ~10-15 |
| Throughput | 370 msgs/s | ~700+ msgs/s |
| Message types | range_request, range_matched, range_events | range_request, range_matched |

## Packet Loss Handling

The verification range_request handles packet loss gracefully:

1. Alice sends 100 blobs + range_request(hash=X)
2. Bob receives only 90 blobs (10 dropped)
3. Bob indexes 90, computes hash=Y (differs from X)
4. Bob sends range_request(hash=Y) back
5. Alice sees hash mismatch, resends all blobs for that leaf
6. Repeat until hashes match

No special retransmission logic needed - the hash comparison naturally detects missing blobs.

## Wire Format

No changes to wire format. We still use:
- `range_request`: level, prefix, hash, range_id, parent_range_id, root_hash, total_events
- `range_matched`: range_id, root_hash, total_events

The `range_events` message type (MSG_RANGE_EVENTS = 3) can be removed from the wire format entirely.
