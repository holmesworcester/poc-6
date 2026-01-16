# V2 Projector Side Effects Analysis

## Overview

9 event types remain without v2 projectors. This doc analyzes their side effects and proposes solutions.

## Current V2 Pattern

```python
EVENT_SPEC = {
    'signer': {...},      # Signature verification
    'requires': {...},    # Required dependencies (block if missing)
    'optional': {...},    # Optional dependencies
}

def project_pure(ctx) -> ProjectorResult:
    # Pure function: ctx in, WriteOps out
    return ProjectorResult(writes=(WriteOp(...),), valid_event=True)
```

The v2 pattern assumes projectors are pure functions that return database writes. Side effects break this model.

---

## Events Needing Conversion

### 1. `group_key_shared`

**Current side effects:**
- Creates derived `group_key` event from shared key material
- Notifies blocked queue (`queues.blocked.notify_event_valid`)
- Retries pending name updates

**Proposed solution:**
- **Key creation**: The receiver needs the key material. Instead of creating a new event during projection, the key material could be written directly to `group_keys` table as a WriteOp. The "event" is the group_key_shared itself - we don't need a separate group_key event on the receiver.
- **Blocked queue notification**: This is a system-level concern. The v2 apply layer could automatically notify after any projection adds to valid_events.
- **Pending name updates**: Could be a separate job that runs after projection, or triggered by the blocked queue notification.

**Complexity**: High - requires rethinking key distribution model

---

### 2. `user_removed`

**Current side effects:**
- Inserts into `removed_users` table ✓ (WriteOp)
- Cascades: marks all user's peers as removed in `removed_peers` ✓ (WriteOp)
- Deletes connections for removed peers (via `connection.remove_connections_for_peer`)
- Rotates group keys (conditional: only if this peer created the event)

**Proposed solution:**
- **Connection deletion**: Express as WriteOp with `op='delete'`
- **Key rotation**: This is the tricky one. Options:
  1. **Sender responsibility**: The remover creates the rotated keys BEFORE creating user_removed. The user_removed event includes references to the new key_ids.
  2. **Deferred job**: Key rotation runs as a background job after removal syncs. Risk: messages sent before rotation use old key.
  3. **Explicit side effect**: Allow project_pure to return `side_effects=[CreateEvent('group_key', {...})]`

**Recommendation**: Option 1 (sender responsibility) is cleanest - rotate keys first, then remove user.

---

### 3. `peer_removed`

**Current side effects:**
- Similar to user_removed but for single peer
- Marks peer as removed
- Deletes connections
- May trigger key rotation if last peer for user

**Proposed solution:** Same as user_removed

---

### 4. `message_deletion`

**Current side effects:**
- Inserts into `message_deletions` table ✓ (WriteOp)
- Marks key for purging (inserts into `keys_to_purge`) ✓ (WriteOp)
- Deletes message from `messages` table ✓ (WriteOp with `op='delete'`)
- Inserts into `deleted_events` table ✓ (WriteOp)
- Cascade deletes from `valid_events` (recursive)
- Deletes from `shareable_events` ✓ (WriteOp)
- Deletes blob from `store` ✓ (WriteOp)

**Proposed solution:**
- Most are already expressible as WriteOps
- **Cascade delete from valid_events**: Could be a SQL trigger or handled by v2 apply layer based on `event_dependencies` table
- This is actually a good v2 candidate!

---

### 5. `message_rekey`

**Current side effects:**
- Re-encrypts message with new key
- Updates message blob in store
- Updates `key_id` in messages table

**Proposed solution:**
- The "re-encryption" happens at create() time, not project() time
- project() just updates the key_id reference
- This should be convertible to v2 with WriteOps

---

### 6. `connection`

**Current side effects:**
- Inserts into `connections` table ✓ (WriteOp)
- Complex address resolution logic
- Ephemeral (not persisted long-term)

**Proposed solution:**
- Address resolution should happen at create() time
- project() is just the table insert
- Should be straightforward v2 conversion

---

### 7. `connection_prekey`

**Current side effects:**
- Stores prekey for connection establishment
- Ephemeral

**Proposed solution:** Straightforward v2 conversion

---

### 8. `sync`

**Current side effects:**
- Updates sync state
- Ephemeral

**Proposed solution:** Straightforward v2 conversion (or skip - sync events may not need projection)

---

### 9. `message_attachment`

**Current side effects:**
- Links attachment metadata to message
- May involve file handling

**Needs investigation:** Check current implementation

---

## Recommendations

### Easy wins (convert now):
1. `connection` - just table insert
2. `connection_prekey` - just table insert
3. `message_deletion` - all WriteOps, cascade can be handled separately
4. `message_rekey` - encryption at create(), projection is just update

### Needs design work:
5. `user_removed` / `peer_removed` - key rotation responsibility
6. `group_key_shared` - key material handling

### Skip for now:
7. `sync` - may not need v2 projection
8. `message_attachment` - needs investigation

---

## Questions for Review

1. **Key rotation timing**: Should key rotation happen before or after removal event? Before is cleaner but requires the remover to create multiple events atomically.

2. **Cascade deletes**: Should cascade deletion from valid_events be:
   - A SQL trigger on valid_events?
   - Handled by v2 apply layer?
   - Explicit in project_pure return value?

3. **Blocked queue notification**: Should this be:
   - Automatic in v2 apply layer after any valid_events insert?
   - Explicit side effect from projector?

4. **group_key_shared derivation**: Should receivers:
   - Create their own group_key event (current)?
   - Just write key material directly to group_keys table?
   - The former maintains event-sourcing purity, the latter is simpler.
