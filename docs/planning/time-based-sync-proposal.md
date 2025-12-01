# Time-Based Sync Protocol Proposal

**Status:** Proposal
**Replaces:** Bloom-filter window-based sync (`sync.py`)

## Summary

Replace the current bloom-filter sync protocol with a simpler, more efficient time-based hierarchical hash protocol inspired by Negentropy. Instead of Negentropy's arbitrary XOR-based interval splitting, we use fixed UTC time intervals that naturally align with how events are created and stored.

## Motivation

The current bloom-filter approach has limitations:

1. **High round count:** Must iterate through 4096 windows to fully sync, one window per round
2. **False positives:** ~9% of events per round are incorrectly skipped, requiring multiple passes
3. **No temporal locality:** Hash-based windows scatter events randomly, can't prioritize recent data
4. **Metadata overhead:** 64-byte bloom per window × 4096 windows = 256KB just for filters

The time-based approach offers:

1. **Fixed 7 rounds max:** Year → Month → Day → Hour → 10min → 1min → events
2. **Deterministic:** No false positives, exact set reconciliation
3. **Temporal locality:** Can sync recent data first, old data is stable
4. **Lower overhead:** Only exchange hashes for buckets that exist

## Protocol Design

### Hierarchy

```
Year        "2024"              12 possible children
  Month     "2024-01"           ~31 possible children
    Day     "2024-01-15"        24 possible children
      Hour  "2024-01-15-14"     6 possible children
        10min "2024-01-15-14-3" 10 possible children
          1min "2024-01-15-14-35" (leaf)
```

### Bucket Hash Computation

**Leaf buckets (1-minute):**
```python
bucket_hash = BLAKE2b-128(concat(sorted(event_ids)))
```

**Parent buckets (hierarchical):**
```python
# Only include non-empty children
child_hashes = [(key, hash) for key, hash in children.items() if hash != EMPTY]
child_hashes.sort(by=key)
bucket_hash = BLAKE2b-128(concat(key + hash for key, hash in child_hashes))
```

This hierarchical approach means:
- Only leaf level touches event IDs
- Parent updates are O(num_children), not O(num_events)
- Adding one event: update 1 leaf + 5 parent hashes = ~100 hash ops total

### Sync Protocol Flow

```
Round 1: Exchange year hashes
         Alice: {2022: h1, 2023: h2, 2024: h3}
         Bob:   {2022: h1, 2023: h2, 2024: h4}  # 2024 differs

Round 2: Exchange month hashes for mismatched years (2024)
         Alice: {2024-01: h5, 2024-02: h6, ...}
         Bob:   {2024-01: h7, 2024-02: h6, ...}  # January differs

Round 3: Exchange day hashes for mismatched months (2024-01)
         ...drill down...

Round 4: Exchange hour hashes for mismatched days

Round 5: Exchange 10-minute hashes for mismatched hours

Round 6: Exchange 1-minute hashes for mismatched 10-minute buckets

Round 7: Send all events in mismatched 1-minute buckets
```

### Leaf Behavior: Send All Events

When a 1-minute bucket hash differs:
- Both peers send all events in that bucket
- Receiver deduplicates (has most already)
- Simple, handles packet loss (just resend bucket)

Typical 1-minute bucket has ~17 events (1000 events/hour ÷ 60).
Worst case with clustering: ~100 events in a busy minute = 50KB.

### Wire Format

**Hash exchange message:**
```python
{
    "type": "sync_time",
    "level": "month",           # year|month|day|hour|ten_min|one_min
    "parent": "2024",           # parent bucket key (null for year level)
    "hashes": {
        "2024-01": "base64(16-byte-hash)",
        "2024-02": "base64(16-byte-hash)",
        ...
    },
    "signed_by": "peer_shared_id",
    "response_key": "base64(transit_key)"  # for encrypted response
}
```

**Event send message:**
```python
{
    "type": "sync_time_events",
    "bucket": "2024-01-15-14-35",  # 1-minute bucket key
    "events": [event1_blob, event2_blob, ...]
}
```

## Integration with Current Design

### Database Schema Changes

**New tables:**
```sql
-- Bucket hashes (hierarchical cache)
CREATE TABLE sync_time_buckets (
    peer_id TEXT NOT NULL,           -- which local peer this is for
    level TEXT NOT NULL,             -- year|month|day|hour|ten_min|one_min
    bucket_key TEXT NOT NULL,        -- "2024-01-15-14-35"
    hash BLOB NOT NULL,              -- 16-byte BLAKE2b hash
    dirty INTEGER DEFAULT 0,         -- needs recomputation
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (peer_id, level, bucket_key)
);

-- Event to bucket mapping (for leaf level)
CREATE TABLE sync_time_events (
    peer_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    bucket_key TEXT NOT NULL,        -- 1-minute bucket
    created_at INTEGER NOT NULL,     -- event timestamp
    PRIMARY KEY (peer_id, event_id)
);
CREATE INDEX idx_sync_time_events_bucket ON sync_time_events(peer_id, bucket_key);
```

**Remove/deprecate:**
- `sync_state_ephemeral` (window tracking)
- `shareable_events.window_id` column
- Bloom filter functions

### Code Changes

**New module:** `events/network/sync_time.py`

```python
# Core functions
def get_bucket_key(ts: int, level: str) -> str:
    """Get bucket key for timestamp at given level."""

def compute_leaf_hash(event_ids: Set[bytes]) -> bytes:
    """BLAKE2b-128 of sorted concatenated event IDs."""

def compute_parent_hash(child_hashes: Dict[str, bytes]) -> bytes:
    """BLAKE2b-128 of sorted (key, hash) pairs."""

def add_event(event_id: str, created_at: int, peer_id: str, db) -> None:
    """Add event to 1-minute bucket, mark ancestors dirty."""

def recompute_dirty_buckets(peer_id: str, db) -> None:
    """Bottom-up recomputation of dirty bucket hashes."""

def get_hashes_at_level(peer_id: str, level: str, parent_key: str, db) -> Dict[str, bytes]:
    """Get all bucket hashes at level, optionally filtered by parent."""

# Protocol functions
def send_sync_request(to_peer: str, from_peer: str, t_ms: int, db) -> None:
    """Initiate sync by sending year hashes."""

def handle_sync_message(msg: dict, from_peer: str, to_peer: str, t_ms: int, db) -> None:
    """Handle incoming sync message, send appropriate response."""
```

**Modify:** `events/network/recorded.py`

```python
def project(...):
    # After marking event valid...

    # Add to time-based sync buckets
    from events.network import sync_time
    sync_time.add_event(event_id, event_data['created_at'], recorded_by, db)
```

**Modify:** `sync.py` or replace entirely

```python
def tick(t_ms: int, db) -> None:
    # Replace bloom-based sync with time-based
    from events.network import sync_time

    # Recompute any dirty bucket hashes before syncing
    for peer_id in get_local_peers(db):
        sync_time.recompute_dirty_buckets(peer_id, db)

    # Send sync requests
    sync_time.send_sync_requests(t_ms, db)
```

### Migration Path

1. **Phase 1:** Add new tables and `sync_time.py` module
2. **Phase 2:** Populate `sync_time_events` from existing `shareable_events`
3. **Phase 3:** Build initial bucket hashes from existing events
4. **Phase 4:** Switch `tick()` to use time-based sync
5. **Phase 5:** Remove bloom-filter code and old tables

### Batching Integration

The current design already batches event processing. Integrate hash updates:

```python
def receive_batch(batch_size: int, t_ms: int, db) -> None:
    # Process events (existing code)
    for blob in incoming_blobs:
        unwrap_and_store(blob, t_ms, db)

    # After batch: recompute dirty hashes once
    for peer_id in affected_peers:
        sync_time.recompute_dirty_buckets(peer_id, db)

    db.commit()
```

This ensures 1000 incoming file slices = 1 hash recomputation pass, not 1000.

## Performance Characteristics

### Sync Efficiency (from model)

| Scenario | Rounds | Hashes | Events Sent | Total |
|----------|--------|--------|-------------|-------|
| Fully synced (1k events) | 1 | 2 | 0 | 40 bytes |
| One missing event | 7 | 103 | 1 | 3.2 KB |
| Dense hour (1k events, 1 diff) | 7 | 40 | 43 | 23 KB |
| Dense hour (1k events, 10 diff) | 7 | 140 | 310 | 159 KB |
| Sparse (20 diff in 10k, 3mo) | 7 | 1173 | 22 | 44 KB |
| New peer joining (5k events) | 7 | 12k | 5000 | 2.9 MB |
| Long-term (200 diff in 50k, 1yr) | 7 | 9648 | 226 | 389 KB |
| **Multi-year (50k over 3yr, 1 diff)** | 7 | 135 | 1 | **4.1 KB** |

### vs Current Bloom Approach

| Metric | Bloom Windows | Time-Based |
|--------|---------------|------------|
| Rounds to full sync | 4096 | 7 |
| Confirm already synced | 4096 rounds, 256KB | 1 round, 40 bytes |
| False positive rate | ~9% | 0% |
| Convergence | Probabilistic | Deterministic |

### Hash Update Cost

Per event: ~100 hash operations (constant, independent of history size)
- 1 leaf rehash: ~17 event IDs
- 5 parent rehashes: 6-31 child hashes each

With batching: 1000 events → ~50 dirty leaves → 50 leaf rehashes + 1 cascade = negligible.

## Open Questions

1. **Bucket key encoding:** Use string keys ("2024-01-15-14-35") or pack into integers?
   - Strings: readable, easy to debug
   - Integers: more compact on wire, faster comparison

2. **Empty bucket handling:** Include empty buckets in parent hash or skip?
   - Current design: skip empty (only hash non-empty children)
   - Ensures two peers with same events get same hashes

3. **Clock skew:** What if peers disagree on current time?
   - Events use `created_at` from event data, not receipt time
   - Bucket membership is deterministic based on event timestamp
   - Skew only affects "current" bucket, old buckets are stable

4. **Backdated events:** Event claims old `created_at`, invalidates cached bucket hash
   - Acceptable: just mark bucket dirty and recompute
   - Validation concerns are separate from sync (handle elsewhere)

5. **Very large 1-minute buckets:** What if 10,000 events in one minute?
   - Send all 10,000 events (~5MB) for that bucket
   - Could add second-level granularity if needed, but probably unnecessary
   - File slices have different timestamps, won't all cluster in one minute

## Appendix: Model Code

See `experiments/time_sync_model_v2.py` for the Python simulation that validates these numbers.

Run with: `python3 experiments/time_sync_model_v2.py`
