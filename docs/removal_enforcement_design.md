# Removal Enforcement: Simplified Connection-Based Design

## Problem Statement

The current user/peer removal implementation creates event records (`removed_users` and `removed_peers` tables) but doesn't prevent removed peers from continuing to sync with the network. The protocol requires:

> "peers check their set of `remove-peer` and `remove-user` events and reject any [Transit-layer Encryption](#transit-layer-encryption) connection, request, or response from removed peers."

**Current Gap**: No actual rejection mechanism exists in the codebase.

## Proposed Solution: Connection-Centric Enforcement

Rather than adding removal checks throughout sync logic, leverage the connection model as the enforcement point:

**Key Insight**: Peers cannot sync without an active connection. If we atomically delete connections when removing a peer, sync becomes impossible without needing additional checks.

### Design Principles

1. **Single Point of Enforcement**: Delete connections in removal events, not sync/connection handlers
2. **Atomic Semantics**: When a peer is removed, all its connections die immediately
3. **No Sync Changes Required**: Sync code doesn't need removal checks if there are no connections
4. **Simple Cascading**: User removal cascades to peer removal, which cascades to connection deletion

## Implementation Plan

### Phase 1: Connection Deletion on Peer Removal

**File**: `events/identity/peer_removed.py`

In the `project()` function, after inserting into `removed_peers` table:

```python
def project(event_id: str, event_data: dict, recorded_by: str, db: Any) -> None:
    """Project peer_removed event to state.

    When a peer is removed:
    1. Insert into removed_peers table
    2. Delete all connections with this peer (prevents future syncs)
    3. Check if last peer of user, rotate keys if needed
    """
    unsafe_db = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    removed_peer_shared_id = event_data.get('removed_peer_shared_id')
    removed_at = event_data.get('created_at')
    removed_by = event_data.get('removed_by')

    if not removed_peer_shared_id:
        return

    # 1. Mark peer as removed globally
    unsafe_db.execute(
        """INSERT OR IGNORE INTO removed_peers (peer_shared_id, removed_at, removed_by)
           VALUES (?, ?, ?)""",
        (removed_peer_shared_id, removed_at, removed_by)
    )

    # 2. DELETE ALL CONNECTIONS with this peer
    #    This is the enforcement mechanism - no connections = no sync possible
    deleted_count = unsafe_db.execute(
        """DELETE FROM sync_connections
           WHERE peer_shared_id = ?""",
        (removed_peer_shared_id,)
    )
    log.info(f"peer_removed.project() deleted {deleted_count} connections for peer {removed_peer_shared_id[:20]}...")

    # 3. Continue with existing cascade logic...
    # [existing code: check if last peer, rotate keys if needed]
```

### Phase 2: Connection Refusal for Removed Peers

**File**: `events/transit/sync_connect.py` (or equivalent connection handler)

When receiving a new `connect` event from a peer, check if the peer is in `removed_peers`:

```python
def project(event_id: str, event_data: dict, recorded_by: str, db: Any) -> None:
    """Project sync_connect event to state.

    Reject connections from removed peers.
    """
    unsafe_db = create_unsafe_db(db)

    peer_shared_id = event_data.get('peer_shared_id')

    if not peer_shared_id:
        return

    # Check if this peer has been removed
    removed = unsafe_db.query_one(
        "SELECT 1 FROM removed_peers WHERE peer_shared_id = ? LIMIT 1",
        (peer_shared_id,)
    )

    if removed:
        log.info(f"sync_connect.project() rejecting connection from removed peer {peer_shared_id[:20]}...")
        return  # Do not create connection entry

    # Proceed with normal connection creation
    # [existing connection creation code]
```

### Phase 3: Verify No Sync Without Connections

**Architectural Assumption**: Confirm that sync events cannot reach a peer without an active connection entry in `sync_connections`.

This should already be true in the current architecture - connections are the prerequisite for sync. If not, that's a separate architectural issue to fix.

## Database Implications

### Tables Involved

1. **removed_peers** (unchanged)
   - Serves as the source of truth for which peers are removed
   - Checked at connection acceptance time

2. **removed_users** (unchanged)
   - User-level removal tracking
   - Cascades to peer removal automatically (via `user_removed.project()`)

3. **sync_connections** (modified)
   - Rows are deleted when their peer is removed
   - No new removal checks needed in sync logic

### Migration

No schema changes needed. The `removed_peers` and `removed_users` tables already exist and are properly indexed.

## Validation & Testing

### Unit Tests

Add to `test_user_removal.py`:

```python
def test_removed_peer_loses_all_connections():
    """When a peer is removed, all its connections are deleted."""
    # Setup: Alice and Bob with active connection
    # Verify connection exists
    # Action: Remove Bob's peer
    # Assert: Bob's connection deleted from sync_connections
    # Assert: Connection count is 0

def test_removed_peer_cannot_connect():
    """A removed peer cannot establish new connections."""
    # Setup: Alice removes Bob
    # Action: Bob tries to send a sync_connect event
    # Assert: Connection entry is not created
    # Assert: sync_connections remains empty for Bob

def test_user_removal_cascades_to_connections():
    """Removing a user removes all connections from all their peers."""
    # Setup: User with 2 devices (2 peers)
    # Verify both peers have connections
    # Action: Remove user
    # Assert: Both peers' connections deleted
    # Assert: Key rotations triggered for all groups
```

### Scenario Tests

Extend existing tests to verify:
1. Post-removal sync cannot occur (no connections = no sync possible)
2. Removed peer's own sync requests are ignored
3. Multi-peer user removal deletes all connections

## Alignment with ideal_protocol_design.md

| Requirement | How Satisfied |
|------------|--------------|
| "reject any Transit-layer Encryption connection, request, or response from removed peers" | ✓ Connection deletion prevents requests; refusal at validation prevents new connections |
| "projection of the removed_user event deletes connection information" | ✓ Connection deletion implemented in `peer_removed.project()` |
| "To ensure a convergent historical record, events from removed users are still valid" | ✓ No change - events remain in store, just unreachable via sync |

## Benefits of This Approach

1. **Simplicity**: Single point of enforcement (connection deletion)
2. **Correctness**: Removed peers literally cannot sync without connections
3. **Performance**: No per-sync-event checks needed
4. **Atomicity**: Removal and enforcement are one operation
5. **Testability**: Easy to verify: "do connections exist?" vs "is peer removed?"

## Edge Cases Handled

1. **Peer removes themselves**: Their own connections deleted, they go offline
2. **Admin removes a user**: All their peers lose connections, all groups re-key
3. **Peer removed while trying to connect**: Connection validation rejects it
4. **Removed peer rejoins**: Only possible if invited again with new invite flow
5. **Offline peer later sees removal**: When they come online and sync removal event, their connections get deleted

## Future Considerations

- Transit keys stored per-connection are implicitly purged when connections are deleted
- Consider adding connection deletion logging for audit trails
- Monitor performance of sync_connections queries if table grows large (add indexes if needed)
- Consider whether removed peers should also have their `transit_prekeys_shared` entries deleted

---

**Status**: Design ready for implementation
**Complexity**: Low (~20 lines of actual code changes)
**Risk**: Very low (mostly deletion operations, minimal behavioral changes)
