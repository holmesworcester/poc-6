# Design: Moving `created_at` and `ttl_ms` Outside the Crypto Wrapper

## Problem Statement

Two related problems stem from having `created_at` and `ttl_ms` inside the encrypted wrapper:

### Problem 1: Negentropy Sync Detection Fails

The negentropy sync protocol needs deterministic event ordering across all peers. Events use a `unified_key` for bucket assignment that includes the timestamp. For encrypted events, the `created_at` timestamp is inside the encrypted payload and unavailable until decryption, causing peers to compute different keys for the same event.

### Problem 2: Blocked Events Never Expire

Blocked events (waiting for dependencies or decryption keys) cannot self-expire because their `ttl_ms` is encrypted. This causes:
- Crypto-blocked events sync back and forth indefinitely
- Blocked event queues grow unbounded
- Server helpers cannot enforce TTL

### Spec Deviation

The spec (`quiet-protocol-specification.md:473`) explicitly states:

> "`created_at` and `ttl` live **outside this encryption layer** so that peers can support lazy loading."

**The current implementation deviates from spec** by encrypting these fields inside the JSON payload.

### Current Behavior

```
Event structure:
┌─────────────────────────────────────────┐
│ Signature (covers everything below)     │
├─────────────────────────────────────────┤
│ Encrypted Wrapper                       │
│ ┌─────────────────────────────────────┐ │
│ │ created_at: 1700000000000           │ │
│ │ event_type: "message"               │ │
│ │ payload: { content: "Hello" }       │ │
│ └─────────────────────────────────────┘ │
└─────────────────────────────────────────┘
```

When a peer receives an encrypted event they can't decrypt, they fall back to `recorded_at` (local receipt timestamp). Different peers receive events at different times, resulting in different `recorded_at` values:

```
Same event "abc123":
  Alice: unified_key = "00018b3c5f40" + hash("abc123")  # created_at
  Bob:   unified_key = "00018b3c9000" + hash("abc123")  # recorded_at (different!)
```

This causes:
1. Different bucket assignments per peer
2. Different root hashes
3. Sync detection never reports "complete" (root hashes never match)
4. Tests run to max_rounds before timing out

### Alternative Solution: Hash-Only Buckets

The simpler solution (implemented in `hash-only-buckets` branch) removes timestamp from `unified_key` entirely, using only `hash(event_id)`. This works but loses temporal locality benefits.

## Proposed Solution: External Envelope Metadata

Move `created_at` and `ttl_ms` outside the encrypted wrapper but keep them inside the signature:

```
Event structure:
┌─────────────────────────────────────────┐
│ Signature (covers everything below)     │
├─────────────────────────────────────────┤
│ created_at: 1700000000000               │  ← MOVED OUTSIDE
│ ttl_ms: 604800000 (7 days)              │  ← MOVED OUTSIDE
├─────────────────────────────────────────┤
│ Encrypted Wrapper                       │
│ ┌─────────────────────────────────────┐ │
│ │ event_type: "message"               │ │
│ │ payload: { content: "Hello" }       │ │
│ └─────────────────────────────────────┘ │
└─────────────────────────────────────────┘
```

### Security Analysis

**What is preserved:**
- **Authenticity**: `created_at` is still inside the signature
- **Integrity**: Tampering with `created_at` invalidates the signature
- **Non-repudiation**: The creator committed to this timestamp

**What changes:**
- **Confidentiality of timing**: Anyone can see when an event was created
- **Metadata exposure**: Reveals activity patterns even without content access

**Threat scenarios:**

| Threat | Current | Proposed | Notes |
|--------|---------|----------|-------|
| Network observer sees content | ✗ (encrypted) | ✗ (encrypted) | No change |
| Network observer sees when events happen | ✗ (inside wrapper) | ✓ (visible) | **Metadata leak** |
| Adversary modifies timestamp | ✗ (sig check fails) | ✗ (sig check fails) | No change |
| Replay attacks | Handled by event_id uniqueness | Handled by event_id uniqueness | No change |
| Traffic analysis | Packet timing reveals activity | Both reveal activity | Minor increase |

**Risk assessment:**
- Low-to-medium risk for most use cases
- High risk for: whistleblowers, activists, anyone where timing metadata is sensitive
- Most messaging apps (Signal, etc.) already leak timing metadata

## Implementation Design

### 1. Event Storage Schema Changes

```sql
-- events_encrypted table
ALTER TABLE events_encrypted ADD COLUMN created_at INTEGER;
-- Move from inside payload to outside

-- Backward compatibility: existing events have created_at = NULL (inside wrapper)
-- New events have created_at populated
```

### 2. Event Creation Flow

```python
# Current flow in store.event():
def event(event_type, payload, peer_id, t_ms, db):
    # 1. Build inner payload with created_at inside
    inner = {
        'event_type': event_type,
        'created_at': t_ms,
        'payload': payload
    }
    # 2. Encrypt
    encrypted = encrypt(inner, group_key)
    # 3. Sign encrypted blob
    signature = sign(encrypted, peer_private_key)
    # 4. Store

# Proposed flow:
def event(event_type, payload, peer_id, t_ms, db):
    # 1. Build inner payload WITHOUT created_at
    inner = {
        'event_type': event_type,
        'payload': payload
    }
    # 2. Encrypt
    encrypted = encrypt(inner, group_key)
    # 3. Build signable blob WITH created_at outside
    signable = {
        'created_at': t_ms,
        'encrypted': encrypted
    }
    # 4. Sign the combined structure
    signature = sign(canonicalize(signable), peer_private_key)
    # 5. Store with created_at outside
```

### 3. Event Verification Flow

```python
# Current verification:
def verify_event(event_blob, signature, public_key):
    return verify(event_blob, signature, public_key)

# Proposed verification:
def verify_event(created_at, encrypted_blob, signature, public_key):
    # Reconstruct signable structure
    signable = {
        'created_at': created_at,
        'encrypted': encrypted_blob
    }
    return verify(canonicalize(signable), signature, public_key)
```

### 4. Negentropy Integration

```python
# In shareable_events table:
# created_at is now always available without decryption

def compute_unified_key(event_id, created_at):
    # No fallback to recorded_at needed!
    # created_at is always available from outer wrapper
    ts_hex = format(created_at & 0xFFFFFFFFFFFF, '012x')
    hash_hex = blake2b(event_id, digest_size=2).hexdigest()
    return ts_hex + hash_hex
```

### 5. Migration Strategy

**Phase 1: Dual Support**
- New events: `created_at` outside wrapper
- Old events: `created_at` inside wrapper (NULL outside)
- Sync code: Check outer `created_at` first, fall back to inner if NULL

**Phase 2: Gradual Migration**
- When processing old events, optionally re-wrap them
- Not required for correctness, but improves consistency

**Phase 3: Deprecation**
- After sufficient time, stop supporting inner-only `created_at`
- Reject events without outer `created_at`

### 6. Wire Format

Current 512-byte blob format:
```
[0-49]    Header (50 bytes) - currently unused/padding
[50-447]  Payload (id + nonce + ciphertext OR plaintext + padding)
[448-511] Signature (64 bytes)
```

Proposed format - use header bytes for external metadata:
```
[0-7]     created_at (8 bytes, uint64 ms timestamp)
[8-15]    ttl_ms (8 bytes, uint64 ms, 0 = no expiry)
[16-49]   Reserved/padding (34 bytes)
[50-447]  Payload (unchanged)
[448-511] Signature (64 bytes) - NOW COVERS created_at + ttl_ms
```

This uses the existing 50-byte header for metadata while maintaining backward compatibility with the overall 512-byte structure.

## Files to Modify

| File | Change |
|------|--------|
| `core/crypto.py` | Update `sign()` and `verify()` to include `created_at` |
| `store.py` | Pass `created_at` through signing process |
| `schema.py` | Add `created_at` column to relevant tables |
| `events/network/recorded.py` | Update event recording to extract `created_at` |
| `events/network/negentropy.py` | Use outer `created_at` (no fallback needed) |
| `core/unwrap.py` | Extract `created_at` from outer structure |

## Testing Strategy

1. **Unit tests**: Verify signature includes `created_at`
2. **Integration tests**: Verify sync converges with new format
3. **Migration tests**: Verify old events still work
4. **Security tests**: Verify signature fails if `created_at` tampered

## Benefits Beyond Sync Detection

Moving `created_at` and `ttl_ms` outside the wrapper provides additional benefits:

### 1. Blocked Events Self-Purge

```python
def should_purge_blocked_event(event_blob, current_time_ms):
    created_at, ttl_ms = extract_envelope_metadata(event_blob)
    if ttl_ms == 0:
        return False  # No expiry
    return created_at + ttl_ms < current_time_ms
```

This prevents infinite sync loops - both peers independently decide to purge at the same time.

### 2. Server Helpers Can Enforce TTL

Server-side relay helpers can drop expired events without decryption keys, reducing bandwidth and storage.

### 3. Lazy Loading UI

UI can sort events by `created_at` without decrypting content, enabling:
- "Jump to date" navigation
- Efficient timeline rendering
- Background decryption of visible events only

### 4. Matches Spec Intent

This change fulfills the original design goal stated in the spec.

## Comparison with Hash-Only Approach

| Aspect | Hash-Only Buckets | External Envelope |
|--------|-------------------|-------------------|
| Implementation complexity | Low | Medium-High |
| Migration complexity | Low | Medium |
| Temporal locality | Lost | Preserved |
| Metadata exposure | None | Timing visible |
| Root hash matching | Works | Works |
| Event ordering | Hash-based (random) | Time-based (natural) |
| Query efficiency | O(log n) all ranges | O(1) for recent events |
| Blocked event TTL | Requires fallback | Native support |
| Server-side expiry | Not possible | Possible |
| Spec compliance | Deviation | Matches spec |

## Recommendation

### Short-term: Hash-Only Buckets

For the immediate test performance issue, **hash-only buckets** is the simpler and safer fix:
- No security implications (no metadata exposure)
- Low implementation complexity
- Already implemented and tested

### Long-term: External Envelope Metadata

For long-term architecture, **external envelope** should be considered because:
- **Matches spec intent** - The spec explicitly calls for this design
- **Solves multiple problems** - Sync detection, blocked event TTL, lazy loading
- **Required for blocked event expiry** - Without this, blocked events accumulate indefinitely

The metadata exposure trade-off is acceptable for most use cases:
- Most messaging apps (Signal, WhatsApp, etc.) already expose timing metadata
- Timing can be inferred from packet timing anyway
- The alternative (unbounded blocked event growth) is worse

## Open Questions

1. **Is timing metadata exposure acceptable for this use case?**
   - Different answer for social chat vs. sensitive communications

2. **Should `created_at` be optional per-event?**
   - Allow senders to opt-in/out of timing exposure
   - Complicates sync but preserves choice

3. **What about clock skew?**
   - Malicious peers could set arbitrary timestamps
   - Need bounds checking (e.g., not >1 hour in future)

4. **Backward compatibility requirements?**
   - How long to support old format?
   - Is forced migration acceptable?

## Next Steps

1. Gather feedback on security/privacy trade-offs
2. Decide on migration strategy
3. Prototype and benchmark temporal locality benefits
4. If proceeding, implement in phases

## Related Documents

- `docs/planning/blocked-events-ttl-analysis.md` - Analysis of blocked event expiry problem
- `quiet-protocol-specification.md:473` - Spec requirement for external metadata
