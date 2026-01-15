# Blocked Events TTL Analysis

## Summary

**Blocked events are NOT purged when TTL expires.** There is no TTL mechanism for blocked events. They persist indefinitely in the ephemeral tables until their dependencies are satisfied or projection succeeds.

## Key Finding: TTL Is Encrypted

The spec states (`quiet-protocol-specification.md:473`):

> "`created_at` and `ttl` live **outside this encryption layer** so that peers can support lazy loading."

**But the implementation encrypts them inside the JSON payload.** When an event is crypto-blocked, we cannot read its `ttl_ms` without the decryption key. This means:

- Crypto-blocked events cannot self-expire
- Server helpers cannot enforce TTL
- Expired crypto-blocked events sync back and forth indefinitely

## Current Behavior

### Storage

Blocked events are stored in two ephemeral tables (`core/queues.sql`):

1. **`blocked_events_ephemeral`** - Main blocked event storage
   - Columns: `recorded_id`, `recorded_by`, `missing_deps` (JSON), `deps_remaining` (counter)
   - Primary key: `(recorded_id, recorded_by)`

2. **`blocked_event_deps_ephemeral`** - Dependency tracking (Kahn's algorithm)
   - Maps each blocked event to its missing dependencies
   - Cascades deletion when parent is removed

### How Events Get Unblocked

Events only leave the blocked queue through:

1. **Dependency resolution** - When all deps become valid, `notify_event_valid()` decrements `deps_remaining` to 0
2. **Successful projection** - After re-projection succeeds, `_cleanup_successfully_projected_events()` removes from queue
3. **Ephemeral event dropping** - Ephemeral events are dropped immediately, never blocked (`recorded.py:392-394`)

### What TTL Exists (Not for Blocked Events)

The `purge_expired.py` module handles TTL for **content events** only:

- Messages (`message_content`)
- Transit prekeys (`transit_prekey`)
- Group prekeys (`group_prekey`)

These use the `ttl_ms` field (absolute timestamp). Blocked events have no equivalent.

## Problem

Blocked events can accumulate indefinitely if:

1. Dependencies are never received (network partition, peer gone offline permanently)
2. Dependencies are invalid/malicious (attacker sends events with impossible deps)
3. Circular dependencies exist (bug in event creation)
4. Peer database corruption causes deps to never project

### Example Scenarios

**Scenario A: Offline peer**
- Peer A sends events depending on events only held by Peer B
- Peer B goes permanently offline
- Events stay blocked forever on Peer A

**Scenario B: Malicious events**
- Attacker sends events referencing non-existent `dep_id`s
- Events block waiting for dependencies that will never arrive
- Blocked queue grows unbounded

**Scenario C: Partial sync**
- Peer receives child events before parent events
- Sync interrupted before parents arrive
- Events blocked until next sync (could be indefinite)

## Potential Solutions

### Option 1: TTL for Blocked Events

Add `blocked_at_ms` timestamp and purge after configurable TTL:

```sql
ALTER TABLE blocked_events_ephemeral
ADD COLUMN blocked_at_ms INTEGER NOT NULL DEFAULT 0;
```

Purge logic in `purge_expired.py`:
```python
DELETE FROM blocked_events_ephemeral
WHERE blocked_at_ms + ? < ?
```

**Pros:** Simple, bounded memory usage
**Cons:** Legitimate slow syncs may lose events, needs careful TTL tuning

### Option 2: Bounded Queue Size

Evict oldest blocked events when queue exceeds limit (LRU):

```python
MAX_BLOCKED_EVENTS = 1000

def evict_oldest_blocked(recorded_by, db):
    count = db.execute("SELECT COUNT(*) FROM blocked_events_ephemeral WHERE recorded_by = ?", (recorded_by,)).fetchone()[0]
    if count > MAX_BLOCKED_EVENTS:
        # Delete oldest entries
```

**Pros:** Guaranteed bounded memory
**Cons:** May evict events that would have unblocked soon

### Option 3: Retry Counter with Backoff

Track retry attempts, eventually give up:

```sql
ALTER TABLE blocked_events_ephemeral
ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0,
ADD COLUMN next_retry_ms INTEGER NOT NULL DEFAULT 0;
```

**Pros:** Gives events multiple chances, adaptive
**Cons:** More complex, still needs eventual purge

### Option 4: Request Missing Dependencies

When events are blocked, actively request dependencies from peers:

```python
def request_missing_deps(blocked_event, peer_id):
    missing = json.loads(blocked_event['missing_deps'])
    send_dep_request(peer_id, missing)
```

**Pros:** Proactive resolution
**Cons:** Doesn't help if deps don't exist, network overhead

## Recommended Solution: External TTL in Envelope

Move `created_at` and `ttl_ms` to the external (unencrypted) envelope layer.

### Wire Format Change

Current 512-byte format:
```
[0-49]    Header (50 bytes) - currently unused/padding?
[50-447]  Payload (id + nonce + ciphertext OR plaintext + padding)
[448-511] Signature (64 bytes)
```

Proposed: Use header bytes for external metadata:
```
[0-7]     created_at (8 bytes, uint64 ms timestamp)
[8-15]    ttl_ms (8 bytes, uint64 ms, 0 = no expiry)
[16-49]   Reserved/padding (34 bytes)
[50-447]  Payload (unchanged)
[448-511] Signature (64 bytes) - NOW COVERS created_at + ttl_ms
```

### Benefits

1. **Blocked events self-purge** - Read TTL without decryption
2. **Server helpers enforce TTL** - Drop expired events server-side
3. **No sync ping-pong** - Expired events purged, not relayed
4. **Lazy loading works** - UI can sort by `created_at` without decrypting
5. **Matches spec intent** - Fulfills the original design goal

### Implementation Steps

1. **Update `crypto.wrap()`** - Write `created_at` and `ttl_ms` to header bytes
2. **Update `crypto.unwrap()`** - Extract header before decryption attempt
3. **Update signature** - Include header in signed data (prevent tampering)
4. **Update `store.event()`** - Pass external metadata, store even if crypto-blocked
5. **Update blocked event tables** - Add `ttl_ms` column from external envelope
6. **Update `purge_expired.py`** - Include blocked events in purge sweep
7. **Update sync logic** - Filter expired events before sending

### Deterministic TTL for Crypto-Blocked Events

With external TTL, even crypto-blocked events have deterministic expiry:

```python
def should_purge_blocked_event(event_blob, current_time_ms):
    created_at, ttl_ms = extract_envelope_metadata(event_blob)
    if ttl_ms == 0:
        return False  # No expiry
    return created_at + ttl_ms < current_time_ms
```

This prevents infinite sync loops - both peers independently decide to purge at the same time.

### Migration

For existing events without external metadata:
- Treat missing header as `ttl_ms = 0` (no expiry)
- Or use a fallback TTL (e.g., 30 days from `recorded_at`)

## Alternative: Fallback TTL Only

If envelope changes are too disruptive, simpler fallback:

1. Add `blocked_at_ms` timestamp when events are blocked
2. During sync, request missing deps from peers that claim to have them
3. After generous TTL (e.g., 7 days), purge blocked events
4. Log purged events for debugging/audit

This is less elegant but doesn't require wire format changes.

## Questions to Resolve

1. What default TTL for events without explicit TTL? (Hours? Days? Weeks?)
2. Should purged blocked events be logged to a separate audit table?
3. Is the 50-byte header actually unused, or is there existing structure?
4. Wire format versioning - how to distinguish old vs new format events?
5. Should server helpers be allowed to drop expired events, or just deprioritize?

## Related Files

| File | Relevance |
|------|-----------|
| `core/queues.sql:15-36` | Blocked event table definitions |
| `core/queues.py:93-357` | Blocking queue operations |
| `events/network/recorded.py:369-407` | Blocking decision logic |
| `events/network/recorded.py:503-582` | Unblocking and cleanup |
| `core/purge_expired.py` | Existing TTL purge pattern |
