# V2 Projector Model

## Core Principle

Projectors are pure functions with well-defined outputs:

```python
def project_pure(ctx) -> ProjectorResult:
    return ProjectorResult(
        writes=(...),           # Table writes (insert/update/delete)
        emit_events=(...),      # New deterministic events to create
        valid_event=True
    )
```

## Output Types

### 1. WriteOp - Table Operations
```python
WriteOp(op='insert', table='messages', values={...})
WriteOp(op='update', table='groups', values={...}, where={...})
WriteOp(op='delete', table='connections', where={...})
```

### 2. EmitEvent - Deterministic Event Creation
Some projectors must emit new events. These are **deterministic** - given the same input, they produce the same event.

```python
EmitEvent(
    event_type='group_key',
    event_data={...},  # Deterministic content
)
```

The apply layer handles creation/storage of emitted events.

---

## Event-Specific Plans

### group_key_shared
**Current**: Creates derived `group_key` event during projection.
**V2**: Emit the deterministic `group_key` event.
```python
def project_pure(ctx) -> ProjectorResult:
    # Decrypt and verify...
    return ProjectorResult(
        writes=(WriteOp(op='insert', table='group_keys_shared', values={...}),),
        emit_events=(EmitEvent(event_type='group_key', event_data={...}),),
    )
```

### connection (ack creation)
**Current**: `_project_request` calls `_send_ack_for_request` to create ack event.
**V2**: Projector stores request; tick job creates ack.

**Why NOT EmitEvent?**
Connection acks contain a **fresh random symmetric key** (`crypto.generate_secret()`).
EmitEvent is for **deterministic** events (same input → same output).
The ack key is randomly generated, making the ack non-deterministic.

**V2 Architecture**:
```python
# Projector (pure) - just stores the pending request
def project_pure(ctx) -> ProjectorResult:
    if mode == 'req':
        return ProjectorResult(
            writes=(WriteOp(op='insert', table='pending_connection_requests', values={...}),),
            # NO emit_events - ack created by tick job
        )
    elif mode == 'ack':
        return ProjectorResult(
            writes=(WriteOp(op='update', table='connections', values={...}, where={...}),),
        )

# Tick job (impure) - creates and sends acks
def send_to_all():
    for pending in pending_connection_requests:
        ack_id, ack_key = create_ack(...)  # Generates fresh key
        wrap_and_queue(ack_id, their_key)
```

**Why this works**:
1. Projector remains pure (no key generation, no network I/O)
2. Ack creation deferred to tick job which already handles pending requests
3. Fresh key generation happens in job context, not projection context
4. Testable: unit test projector purity, integration test job behavior

### message_deletion
**Current**: Cascade deletes from valid_events via recursive `_cascade_delete_from_valid_events()`.
**V2**: Apply layer handles cascade based on `event_dependencies` table.
```python
def project_pure(ctx) -> ProjectorResult:
    return ProjectorResult(
        writes=(
            WriteOp(op='insert', table='message_deletions', values={...}),
            WriteOp(op='insert', table='deleted_events', values={...}),
            WriteOp(op='delete', table='messages', where={'message_id': ...}),
            # Apply layer handles cascade from valid_events
        ),
    )
```

### user_removed / peer_removed
**Current**: Conditionally rotates keys if this peer created the event.
**V2**: Just write the removal. Key rotation is a separate admin action.
```python
def project_pure(ctx) -> ProjectorResult:
    return ProjectorResult(
        writes=(
            WriteOp(op='insert', table='removed_users', values={...}),
            WriteOp(op='insert', table='removed_peers', values={...}),  # for each peer
            WriteOp(op='delete', table='connections', where={'peer_shared_id': ...}),
        ),
    )
```

**Key rotation is sender's responsibility** - see below.

### sync
**Status**: May need impure helper for sync state management. Investigate further.

---

## Key Selection Model

**Principle**: Sender chooses the right key. Rotator just adds new keys to the list.

### Current (wrong)
- Remover rotates keys and tries to distribute new keys
- Old keys get removed/invalidated
- Complex coordination

### V2 (correct)
- Admin creates new group_key when removing a user (rotation)
- All group_keys remain available (historical access)
- **Sender picks latest key by timestamp** when encrypting
- TODO: Change selection from timestamp-based to explicit ordering/chain

### Why this works
1. Removed user can't decrypt messages encrypted with post-removal keys
2. Remaining users always pick latest key (post-removal)
3. No need to coordinate key distribution - keys sync naturally
4. Historical messages remain decryptable with historical keys

---

## Apply Layer Responsibilities

The v2 apply layer handles:

1. **Execute WriteOps** - INSERT/UPDATE/DELETE
2. **Create emitted events** - Store and project deterministic events
3. **Cascade deletes** - When event deleted from valid_events, cascade via event_dependencies
4. **Notify blocked queue** - After valid_events insert, check blocked_events_ephemeral

---

## Implementation Order

1. Add `emit_events` to `ProjectorResult` type
2. Update apply layer to handle emitted events
3. Update apply layer to handle cascade deletes
4. Convert events:
   - `group_key_shared` (emit group_key)
   - `connection` (emit ack)
   - `message_deletion` (cascade handled by apply)
   - `user_removed` / `peer_removed` (just writes, no rotation)
   - `message_rekey` (simple writes)

---

## Open Questions

1. **Sync events**: Pure or impure? Need to investigate sync state management.
2. **Key selection ordering**: Currently timestamp-based. TODO: design explicit ordering.
