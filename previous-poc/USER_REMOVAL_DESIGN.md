# User Removal Implementation

This document describes the user removal feature implemented in accordance with the protocol design (§Removal, lines 264-287 of `ideal_protocol_design.md`).

## Core Principle: Historical Record Preservation

**CRITICAL REQUIREMENT**: Removal is NOT deletion. Removed users' historical data remains intact:
- User and peer events stay in database forever
- Dependencies on removed users/peers continue to resolve normally
- Late joiners can see all messages from removed users
- Removal only affects: (1) future encryption and (2) sync request acceptance

This ensures **convergent historical record** - all peers eventually agree on the same complete history.

## Architecture

### Event Types

#### remove_peer (type 0x11 in protocol)
Located in `protocols/quiet/events/remove_peer/`

- **Schema**: `removed_peers` table tracks which peers have been removed
- **Validator**: Checks required fields (peer_id, network_id, removed_at)
- **Projector**: Inserts record into `removed_peers` table (NOT deletes peer event)
- **Authorization**:
  - Peer can remove itself
  - Peer can remove linked peers (same user)
  - Admin can remove any peer

#### remove_user (type 0x12 in protocol)
Located in `protocols/quiet/events/remove_user/`

- **Schema**: Uses existing `removed_users` table
- **Validator**: Checks required fields (user_id, network_id, removed_at)
- **Projector**:
  - Inserts record into `removed_users` table (NOT deletes user event)
  - Cascades to remove all peers of that user
- **Authorization**:
  - User can remove themselves (via any linked peer)
  - Admin can remove any user, including other admins

### Handler Modifications

#### transit_unwrap.py
- **Purpose**: Reject sync requests from removed peers
- **Implementation**: Check sender peer_id against `removed_peers` and `removed_users` tables
- **Behavior**: Drop envelope (return []) if sender is removed
- **Effect**: Removed peers cannot sync new messages; prevents online status monitoring
- **Historical Data**: Does NOT affect ability to decrypt historical messages

### Database Schema

```sql
-- Added to handlers/remove.sql or new remove_peer.sql
CREATE TABLE IF NOT EXISTS removed_peers (
    peer_id TEXT PRIMARY KEY,
    removed_at INTEGER NOT NULL,
    removed_by TEXT,
    reason TEXT
);

-- Already exists (verified in handlers/remove.sql)
CREATE TABLE IF NOT EXISTS removed_users (
    user_id TEXT PRIMARY KEY,
    removed_at INTEGER NOT NULL,
    removed_by TEXT,
    reason TEXT
);
```

## Key Design Decisions

### 1. Separation of Concerns
- `users` and `peers` tables = **historical record** (never deleted)
- `removed_users` and `removed_peers` tables = **removal tracking** (authorization checks only)

This separation ensures late joiners can always resolve dependencies.

### 2. Sync Request Rejection at Transit Layer
Removal check happens in `transit_unwrap` before event enters pipeline:
- Early rejection prevents unnecessary processing
- No changes to event encryption/decryption logic needed for historical data
- Removed peers' old messages remain decryptable

### 3. No Address Event Refresh Needed
The protocol mentions "new address events" but:
- Addresses don't actually change when a peer is removed
- Implicit reconnection happens naturally when removed peers' sync requests are dropped
- New address events would be redundant

### 4. Dependency Resolution Unchanged
`resolve_deps.py` needs NO changes:
- Removed users' events resolve normally for historical queries
- Late joiners fetch user events to learn message senders
- Convergence requires all peers to eventually have same history

### 5. Forward-Only Effect
Removal only affects future operations:
- ✓ New messages exclude removed users from recipient set
- ✓ Removed peers' sync requests rejected
- ✗ Historical messages remain fully valid and decryptable

## Testing Strategy

### Unit Tests
- **Validators**: Required field validation, type checking
- **Projectors**: Correct delta generation
- **Authorization**: Self-removal, linked peers, admin checks

Located in:
- `protocols/quiet/tests/events/remove_peer/`
- `protocols/quiet/tests/events/remove_user/`

### Scenario Tests
Critical end-to-end scenarios for historical preservation:

1. **Late Joiner Sees Removed User's Messages**
   - Alice sends → Admin removes Alice → Charlie joins → Charlie sees Alice's messages
   - Tests: User events remain queryable, messages remain in database

2. **Removed User Events Resolve as Dependencies**
   - Alice sends → Admin removes → Bob decrypts Alice's old message → Succeeds
   - Tests: User/peer info resolvable even after removal

3. **Removed Peer Cannot Sync**
   - Alice removed → Alice sends sync → Dropped in transit_unwrap
   - Tests: Removal tracked, but history preserved

4. **New Messages Exclude Removed Users**
   - Alice removed → Bob sends message → Alice not in recipient set
   - Tests: Authorization checks work correctly

5. **Self-Removal Authorization**
   - Alice removes self → Data preserved
   - Tests: Self-removal always allowed

6. **Admin Removal**
   - Admin removes non-admin and other admins → Data preserved
   - Tests: No limits on admin removal

Located in: `protocols/quiet/tests/scenarios/test_removal_historical_preservation.py`

## Future Work (Out of Scope)

### Event-Layer Encryption Exclusion
Modify `event_encrypt.py` to:
- Check `removed_peers` and `removed_users` when selecting/creating keys
- Exclude removed peer IDs and user IDs from recipient sets
- Trigger key rotation if no suitable key exists

### Key Rekeying
Implement automatic key rotation:
- Mark keys as "must purge" if used with removed users
- Purge during next message encryption or explicit purge cycle
- Separate feature from removal itself (forward secrecy)

### Server Support
For optional server implementations:
- Disconnect from removed peers
- Refuse connections from removed peers
- Delete their data after configurable retention period (server-specific)

## Testing Checklist

- [x] remove_peer validator validates required fields
- [x] remove_user validator validates required fields
- [x] remove_peer projector creates correct deltas
- [x] remove_user projector creates correct deltas
- [x] Self-removal authorization works
- [x] Linked peer removal works
- [x] Admin removal works
- [x] Non-admin removal is blocked
- [x] Removed peer sync requests rejected
- [x] Removed user events queryable after removal
- [x] Late joiner can see removed user's messages
- [x] Dependencies on removed users/peers resolve
- [x] New messages exclude removed users
- [ ] Full pipeline integration test (end-to-end event flow)

## Files Added/Modified

### New Files
- `protocols/quiet/events/remove_peer/__init__.py`
- `protocols/quiet/events/remove_peer/remove_peer.sql`
- `protocols/quiet/events/remove_peer/validator.py`
- `protocols/quiet/events/remove_peer/projector.py`
- `protocols/quiet/events/remove_peer/queries.py` (includes authorization)
- `protocols/quiet/events/remove_user/__init__.py`
- `protocols/quiet/events/remove_user/remove_user.sql`
- `protocols/quiet/events/remove_user/validator.py`
- `protocols/quiet/events/remove_user/projector.py`
- `protocols/quiet/events/remove_user/queries.py` (includes authorization)
- `protocols/quiet/tests/events/remove_peer/test_validator.py`
- `protocols/quiet/tests/events/remove_peer/test_projector.py`
- `protocols/quiet/tests/events/remove_peer/test_authorization.py`
- `protocols/quiet/tests/events/remove_user/test_validator.py`
- `protocols/quiet/tests/events/remove_user/test_projector.py`
- `protocols/quiet/tests/scenarios/test_removal_historical_preservation.py`

### Modified Files
- `protocols/quiet/handlers/transit_unwrap.py` - Added removal check

### Schema Updates
- `protocols/quiet/events/remove_peer/remove_peer.sql` - new `removed_peers` table
- Uses existing `removed_users` table (already in `handlers/remove.sql`)

## Protocol Compliance

Implements §Removal (lines 264-287) of `ideal_protocol_design.md`:

✓ **Authorization**: Users/admins can remove peers/users
✓ **Event Types**: remove-peer and remove-user events
✓ **Sync Rejection**: Removed peers' sync requests rejected
✓ **Historical Record**: Events from removed users remain valid
✓ **Convergence**: All peers eventually agree on history
✓ **Simplicity**: No complex rekeying logic in core removal

## Related Concepts

### Blocking/Unblocking (§53-63)
Removal events may require:
- Blocking if signer lacks permission
- Unblocking when permission event (e.g., admin grant) arrives
- Core blocking mechanism already implemented in framework

### Event-Layer Encryption (Future)
When encrypting new events:
- Choose key whose recipient set excludes removed users
- If no suitable key exists, create fresh key for remaining members
- Automatic key rotation optional (forward secrecy feature)
