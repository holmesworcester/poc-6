# Joining/Linking Simplification Plan

## Overview

This document outlines a phased refactoring plan to align the current joining/linking implementation with the ideal protocol design described in `ideal_protocol_design.md` and `joining_linking_simplification.md`.

**Core Goals:**
1. Separate connection establishment from sync ("connect then sync" pattern)
2. Unify invite primitives for both joining and linking
3. Eliminate bootstrap as a special case
4. Remove foreign local deps complexity
5. Make join event blocking consistent across all peers

## Current State Analysis

### Sources of Complexity

1. **Separate Bootstrap Mode**
   - Special `send_bootstrap_to_peer()` logic sends all events without bloom filtering
   - `bootstrap_completers` and `network_joiners` tables track bootstrap state
   - Different code paths for first-time joiners vs established peers
   - File: `events/transit/sync.py:582-693`

2. **Bootstrap Prekey Routing Gap**
   - Bootstrap uses invite_prekey to wrap events
   - Routing doesn't check `group_prekeys` table for invite prekeys
   - Documented bug in `BOOTSTRAP_PREKEY_FIX.md`
   - File: `events/transit/sync.py:235-271` (routing)

3. **Foreign Local Deps Pattern**
   - Complex logic to skip validation of creator's local-only data
   - Special cases for `peer`, `transit_prekey`, `group_prekey`, etc.
   - File: `events/transit/recorded.py:67-100`

4. **One-Way Sync**
   - Requester initiates, responder only responds
   - No automatic two-way connection establishment
   - Requires bootstrap to "push" initial data to joiner
   - File: `events/transit/sync.py:696-1018`

5. **Different Creation Paths**
   - Network creator: `user.new_network()` - special first-user path
   - User join: `user.join()` - bootstrap validation skip
   - Device linking: `link.join()` - similar to user join but different events
   - Files: `events/identity/user.py`, `events/identity/link.py`

6. **Invite Proof Embedded in User/Link Events**
   - Tight coupling between user validation and invite validation
   - Can't validate invite proof until user/link event is created
   - Different proof formats for user vs link
   - Files: `events/identity/user.py:74-92`, `events/identity/link.py:88-103`

### Current File Structure

**Join Flow:**
- `events/identity/user.py` - `user.join()`, `user.new_network()`
- `events/identity/invite.py` - `invite.create()`, validation/projection
- `events/identity/invite_accepted.py` - stores invite secrets locally

**Link Flow:**
- `events/identity/link.py` - `link.join()`, `link.create()`
- `events/identity/link_invite.py` - `link_invite.create()`
- `events/identity/link_invite_accepted.py` - stores link secrets locally

**Sync Implementation:**
- `events/transit/sync.py` - all sync logic (request/response/bootstrap)
- `events/transit/recorded.py` - projection + dependency resolution
- `events/transit/transit_key.py` - ephemeral transit keys

**Bootstrap Tracking:**
- `events/identity/network_joined.py` - marks joiner intent
- `events/identity/bootstrap_complete.py` - marks received sync
- `events/identity/network_created.py` - marks creator

**Tests:**
- `tests/scenario_tests/test_three_player_messaging.py` - three-way sync test
- `tests/scenario_tests/test_multi_device_linking.py` - device linking test
- `tests/scenario_tests/test_message_expiry.py` - TTL/purging test

---

## Phase 1: Connect-Then-Sync Pattern (Foundation)

**Goal**: Separate connection establishment from sync to enable two-way connections without bootstrap special cases.

### Rationale

Currently, sync requests go directly to peers using prekeys, and each request includes a fresh response_transit_key. This one-way pattern requires bootstrap to "push" initial data to new joiners. By establishing explicit connections first, we enable:
- Two-way sync without special bootstrap logic
- Authentication before sync (using invite signature)
- Persistent transit keys for efficient ongoing communication

### 1.1 Create `sync_connect` Event & Connection Tracking

**New Event Type**: `sync_connect` (ephemeral, not stored in event log)

**Fields:**
```python
{
    'type': 'sync_connect',
    'created_by': str,              # sender's peer_shared_id
    'invite_id': str,               # optional, for invite-based auth
    'invite_signature': str,        # optional, signature by invite_private_key
    'response_transit_key_id': str, # hint for reverse encryption
    'response_transit_key': str,    # actual key material (base64)
    'created_at': int,
}
```

**Signature Message** (for invite_signature):
```python
canonical_json({
    'created_by': peer_shared_id,
    'invite_id': invite_id,
    'created_at': created_at_ms
})
```

**New Table**: `sync_connections` (ephemeral, no recorded_by - device-wide)

```sql
CREATE TABLE IF NOT EXISTS sync_connections (
    peer_shared_id TEXT PRIMARY KEY,        -- remote peer
    response_transit_key_id TEXT NOT NULL,  -- how to send to them
    response_transit_key BLOB NOT NULL,     -- key material
    address TEXT,                            -- origin IP address
    port INTEGER,                            -- origin port number
    last_seen_ms INTEGER NOT NULL,
    ttl_ms INTEGER NOT NULL
);

CREATE INDEX idx_sync_connections_last_seen
ON sync_connections(last_seen_ms);
```

**Implementation Steps:**

1. Create `events/transit/sync_connect.py`:
   - `create()` - generates connect event with invite auth
   - `project()` - validates signature, inserts/updates `sync_connections`
   - Validation: accept if peer_shared signature OR invite signature verifies

2. Update `events/transit/sync.py`:
   - Add `send_connect_to_all()` function - sends connects to:
     - Inviter address (from `invite_accepteds` table)
     - All known peer addresses (from `peers_shared` table)
   - Seal connect events to recipient's transit_prekey

3. Add connection lifecycle management:
   - Expire old connections (TTL-based)
   - Refresh active connections periodically
   - Track last_seen_ms on each received connect

**Files to Create/Modify:**
- NEW: `events/transit/sync_connect.py` (~200 lines)
- NEW: `events/transit/sync_connect.sql` (table schema)
- MODIFY: `events/transit/sync.py` (add send_connect_to_all)

### 1.2 Modify Sync to Use Established Connections

**Goal**: Make sync use connections from `sync_connections` table instead of directly targeting prekeys.

**Implementation Steps:**

1. Update `send_request_to_all()`:
   ```python
   def send_request_to_all(...):
       # Query sync_connections for active peers
       connections = db.execute("""
           SELECT peer_shared_id, response_transit_key_id, response_transit_key
           FROM sync_connections
           WHERE last_seen_ms > ? - ttl_ms
       """, (t_ms,)).fetchall()

       for conn in connections:
           # Create sync request
           # Wrap with conn.response_transit_key (symmetric)
           # Add to outgoing queue with hint=conn.response_transit_key_id
   ```

2. Add "reflected sync" logic:
   - When receiving first sync request from a peer, automatically send one back
   - This establishes two-way sync without waiting for periodic send
   - Track "have sent sync to this peer" to avoid ping-pong

3. Remove special bootstrap send logic:
   - Delete `send_bootstrap_to_peer()` function
   - Remove bootstrap mode checks in `send_request_to_all()`
   - Normal sync with bloom filtering handles everything

**Files to Modify:**
- `events/transit/sync.py:696-802` (send_request)
- `events/transit/sync.py:804-918` (project - add reflected sync)
- `events/transit/sync.py:582-693` (DELETE send_bootstrap_to_peer)

### 1.3 Update Tests

**Goal**: Verify connect-then-sync pattern works with existing scenarios.

**Test Updates:**

1. Modify `test_three_player_messaging.py`:
   ```python
   # After join, add explicit connection phase
   sync_connect.send_connect_to_all(t_ms=2100, db=db)
   sync.receive(batch_size=20, t_ms=2150, db=db)

   # Then sync as normal
   for round in range(5):
       sync.send_request_to_all(...)
       sync.receive(...)
   ```

2. Add new test `test_sync_connect.py`:
   - Test connection establishment
   - Test invite-based auth in connect
   - Test connection expiry/refresh

3. Verify existing tests still pass:
   - `test_multi_device_linking.py`
   - `test_message_expiry.py`

**Files to Modify:**
- `tests/scenario_tests/test_three_player_messaging.py`
- NEW: `tests/scenario_tests/test_sync_connect.py`

### Phase 1 Success Criteria
- [ ] `sync_connect` event type created and tested
- [ ] `sync_connections` table tracks active connections
- [ ] Sync uses connections instead of direct prekey targeting
- [ ] Reflected sync establishes two-way communication
- [ ] All existing scenario tests pass with connect-then-sync pattern

---

## Phase 2: Unified Invite Primitive

**Goal**: Use single `invite` event type for both joining and linking, with mode field.

### Rationale

Currently, `invite` and `link_invite` are separate event types with duplicate logic. The ideal protocol design uses a single `invite` primitive with a mode field. This reduces code duplication and makes the system easier to reason about.

### 2.1 Extend `invite` Event Schema

**Current `invite` fields:**
```python
{
    'type': 'invite',
    'invite_pubkey': str,
    'invite_prekey_id': str,
    'network_id': str,
    'group_id': str,
    'channel_id': str,
    'key_id': str,
    # ... transit prekey info
}
```

**Add to `invite` schema:**
```python
{
    # Existing fields...
    'mode': 'user' | 'link',  # NEW: determines invite type
    'user_id': str,           # NEW: for mode='link', target user
}
```

**Validation rules:**
```python
# mode='user' invites:
- Must be created by admin (check admin table)
- user_id field must be empty/null

# mode='link' invites:
- Must be created by peer linked to user_id
- user_id field must be set
```

**Implementation Steps:**

1. Update `events/identity/invite.py`:
   - Add `mode` and `user_id` fields to schema
   - Update validation to check mode-specific rules
   - Keep backward compatibility (default mode='user' if not specified)

2. Update `invite.create()`:
   ```python
   def create(mode='user', user_id=None, ...):
       if mode == 'link':
           assert user_id is not None
           # Verify creator is linked to user_id
       else:
           assert user_id is None
           # Verify creator is admin
   ```

**Files to Modify:**
- `events/identity/invite.py` (add mode field)
- `events/identity/invite.sql` (update schema)

### 2.2 Create Standalone `invite_proof` Event

**Goal**: Separate invite proof logic from `user` and `link` events into a standalone `invite_proof` event.

**New Event Type**: `invite_proof` (shareable, signed by joiner)

**Fields:**
```python
{
    'type': 'invite_proof',
    'invite_id': str,               # which invite this proves
    'mode': 'user' | 'link',        # must match invite.mode
    'joiner_peer_shared_id': str,   # joiner's identity
    'invite_signature': str,        # Ed25519 sig by invite_private_key

    # For mode='user':
    'user_id': str,                 # joiner's user event id

    # For mode='link':
    'link_user_id': str,            # target user being linked to

    'created_by': str,              # joiner_peer_shared_id
    'created_at': int,
}
```

**Signature Messages:**
```python
# mode='user':
canonical_json({
    'invite_id': invite_id,
    'joiner_peer_shared_id': peer_shared_id,
    'user_id': user_id
})

# mode='link':
canonical_json({
    'invite_id': invite_id,
    'joiner_peer_shared_id': peer_shared_id,
    'link_user_id': user_id
})
```

**Validation Rules:**
```python
def validate(event, db):
    # Load invite event
    invite = get_invite(event['invite_id'], db)

    # Verify mode matches
    assert event['mode'] == invite['mode']

    # Verify invite signature
    verify_signature(
        event['invite_signature'],
        invite['invite_pubkey'],
        signature_message
    )

    # Mode-specific checks
    if mode == 'user':
        assert event['user_id'] is not None
        # Verify user event exists and matches
    else:
        assert event['link_user_id'] is not None
        # Verify target user exists
```

**Projection:**
```python
def project(event, db):
    if event['mode'] == 'user':
        # Insert into users table
        # Insert into group_members (for group_id from invite)
        # Mark user_id as valid
    else:
        # Insert into linked_peers table
        # Link joiner_peer_shared_id to link_user_id
        # Mirror users row for new device
```

**New Table**: `invite_proofs` (optional, for debugging/queries)

```sql
CREATE TABLE IF NOT EXISTS invite_proofs (
    invite_proof_id TEXT NOT NULL,
    invite_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('user','link')),
    joiner_peer_shared_id TEXT NOT NULL,
    user_id TEXT,            -- for mode='user'
    link_user_id TEXT,       -- for mode='link'
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (invite_proof_id, recorded_by)
);

CREATE INDEX idx_invite_proofs_invite
ON invite_proofs(invite_id, recorded_by);
```

**Implementation Steps:**

1. Create `events/identity/invite_proof.py`:
   - Define schema with mode-specific fields
   - Implement validation (signature + mode checks)
   - Implement projection (users/linked_peers)
   - Add dependencies: `deps=['invite_id', 'user_id' or 'link_user_id']`

2. Create `events/identity/invite_proof.sql`:
   - Define `invite_proofs` table schema

**Files to Create:**
- NEW: `events/identity/invite_proof.py` (~300 lines)
- NEW: `events/identity/invite_proof.sql` (table schema)

### 2.3 Update Join Flows

**Goal**: Modify `user.join()` and `link.join()` to create `invite_proof` separately.

**user.join() changes:**

Current flow:
```python
# user.py:485-493
user_event = {
    'invite_pubkey': ...,
    'invite_signature': sign(peer_shared_id + ":" + invite_id),
    # proof embedded in user event
}
```

New flow:
```python
# 1. Create user event (no invite proof)
user_event = {
    'type': 'user',
    'name': name,
    # NO invite_pubkey or invite_signature
}
user_id = emit_event(user_event)

# 2. Create separate invite_proof
invite_proof_event = {
    'type': 'invite_proof',
    'invite_id': invite_id,
    'mode': 'user',
    'joiner_peer_shared_id': peer_shared_id,
    'user_id': user_id,
    'invite_signature': sign_proof(invite_id, peer_shared_id, user_id)
}
emit_event(invite_proof_event)
```

**link.join() changes:**

Current flow:
```python
# link.py:335-344
link_event = {
    'user_id': user_id,
    'invite_signature': sign(peer_shared_id + ":" + user_id),
    # proof embedded in link event
}
```

New flow:
```python
# 1. Link event stays simple (just references user)
link_event = {
    'type': 'link',
    'user_id': user_id,
    # NO invite signature
}
emit_event(link_event)

# 2. Create separate invite_proof
invite_proof_event = {
    'type': 'invite_proof',
    'invite_id': invite_id,
    'mode': 'link',
    'joiner_peer_shared_id': peer_shared_id,
    'link_user_id': user_id,
    'invite_signature': sign_proof(invite_id, peer_shared_id, user_id)
}
emit_event(invite_proof_event)
```

**Implementation Steps:**

1. Modify `events/identity/user.py:485-524`:
   - Remove invite_pubkey/invite_signature from user event
   - Add `invite_proof.create()` call after user creation
   - Update validation to not expect invite proof in user

2. Modify `events/identity/link.py:335-395`:
   - Remove invite_signature from link event
   - Add `invite_proof.create()` call after link creation
   - Update validation accordingly

3. Update user/link validation:
   - Remove invite proof validation from user.validate()
   - Remove invite proof validation from link.validate()
   - All proof validation happens in invite_proof.validate()

**Files to Modify:**
- `events/identity/user.py` (remove embedded proof, add invite_proof.create)
- `events/identity/link.py` (remove embedded proof, add invite_proof.create)

### 2.4 Consolidate Invite Types

**Goal**: Deprecate `link_invite` in favor of `invite` with mode='link'.

**Migration Strategy:**

1. **Phase 2.4a**: Support both formats (backward compatibility)
   - Keep `link_invite` event type working
   - New links use `invite` with mode='link'
   - Both project to same tables

2. **Phase 2.4b**: Update link URL generation
   ```python
   # OLD: link.create_invite()
   link_invite_event = {'type': 'link_invite', ...}

   # NEW: invite.create() with mode='link'
   invite_event = {'type': 'invite', 'mode': 'link', 'user_id': user_id, ...}
   ```

3. **Phase 2.4c**: Migrate existing data (optional)
   - Convert `link_invites` table rows to `invites` with mode='link'
   - Update any stored link URLs to new format

**Implementation Steps:**

1. Update `events/identity/link.py:create_invite()`:
   - Change to call `invite.create(mode='link', user_id=user_id)`
   - Remove `link_invite.create()` call

2. Update URL encoding:
   - Change link URL format to include mode field
   - Keep backward compatibility in URL parsing

3. Eventually delete (future phase):
   - `events/identity/link_invite.py`
   - `events/identity/link_invite_accepted.py`
   - `link_invites` table

**Files to Modify:**
- `events/identity/link.py` (use invite.create instead of link_invite.create)
- `events/identity/invite.py` (support mode='link' in URL encoding)

### Phase 2 Success Criteria
- [ ] `invite` event supports both mode='user' and mode='link'
- [ ] `invite_proof` is standalone event, validates consistently
- [ ] `user.join()` creates separate `invite_proof` event
- [ ] `link.join()` creates separate `invite_proof` event
- [ ] `link_invite` still works (backward compatibility)
- [ ] All scenario tests pass with new invite_proof pattern

---

## Phase 3: Remove Bootstrap Special Case

**Goal**: Use normal sync with invite authentication instead of special bootstrap mode.

### Rationale

Currently, bootstrap is a special mode where the inviter sends ALL events without bloom filtering to the joiner. This requires:
- Separate `send_bootstrap_to_peer()` logic
- Bootstrap completion tracking (`bootstrap_completers`, `network_joiners`)
- Special prekey wrapping (invite_prekey instead of transit_prekey)

By adding invite authentication to sync requests, we can:
- Let joiner send normal sync requests (authenticated by invite signature)
- Let inviter respond with normal bloom-filtered sync responses
- Remove all bootstrap-specific code

### 3.1 Add Invite Auth to Sync Requests

**Goal**: Allow sync requests to be authenticated by invite signature, not just peer signature.

**Extend `sync` event schema:**

```python
{
    'type': 'sync',
    # Existing fields...
    'window_id': int,
    'bloom': bytes,
    'response_transit_key': str,

    # NEW: Optional invite authentication
    'invite_id': str,          # optional
    'invite_signature': str,   # optional, Ed25519 by invite_private_key
}
```

**Signature message** (for invite_signature):
```python
canonical_json({
    'created_by': peer_shared_id,
    'invite_id': invite_id,
    'window_id': window_id,
    'created_at': created_at_ms
})
```

**Update sync projection validation:**

```python
def project(sync_event, db):
    # Accept if EITHER:
    # 1. Signed by recognized peer_shared_id, OR
    # 2. Signed by valid invite_private_key

    peer_valid = verify_peer_signature(sync_event)
    invite_valid = False

    if 'invite_id' in sync_event and 'invite_signature' in sync_event:
        invite = get_invite(sync_event['invite_id'], db)
        invite_valid = verify_signature(
            sync_event['invite_signature'],
            invite['invite_pubkey'],
            signature_message
        )

    if not (peer_valid or invite_valid):
        return  # Reject sync request

    # Process normally...
    send_response(...)
```

**Implementation Steps:**

1. Update `events/transit/sync.py`:
   - Add `invite_id` and `invite_signature` fields to sync event schema
   - Update `create()` to optionally include invite auth
   - Update `project()` to accept invite-authenticated requests

2. Update `send_request_to_all()`:
   - Check if this peer has an `invite_accepted` event
   - If yes, include invite_id and sign with invite_private_key
   - This allows sending sync requests before peer_shared is known

3. Rate limiting for reflected sync:
   - Track "have sent reflected sync to this peer"
   - Only send one reflected sync per connection
   - Avoid ping-pong with invite-authenticated requests

**Files to Modify:**
- `events/transit/sync.py:696-802` (add invite auth to requests)
- `events/transit/sync.py:804-918` (validate invite auth)

### 3.2 Project Invite Data to `invite_accepteds` Table

**Goal**: Store inviter's address and transit_prekey for connection attempts.

**New Table**: `invite_accepteds`

```sql
CREATE TABLE IF NOT EXISTS invite_accepteds (
    invite_id TEXT NOT NULL,
    inviter_peer_shared_id TEXT NOT NULL,
    address TEXT,                       -- Inviter IP address
    port INTEGER,                       -- Inviter port number
    inviter_transit_prekey_id TEXT,
    inviter_transit_prekey_public_key BLOB,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (invite_id, recorded_by)
);

CREATE INDEX idx_invite_accepteds_recorded_by
ON invite_accepteds(recorded_by);
```

**Update `invite_accepted.project()`:**

```python
def project(event, db):
    # Existing: store invite_private_key in group_prekeys
    # ... existing code ...

    # NEW: Extract and store inviter metadata
    invite_blob = get_blob(event['invite_id'], db)
    invite_data = json.loads(invite_blob)

    # Get OOB data from invite link
    inviter_address = event.get('inviter_address')  # from URL
    inviter_port = event.get('inviter_port')  # from URL
    inviter_transit_prekey_id = invite_data.get('inviter_transit_prekey_id')
    inviter_transit_prekey_public_key = invite_data.get('inviter_transit_prekey_public_key')

    # Insert into invite_accepteds table
    db.execute("""
        INSERT OR REPLACE INTO invite_accepteds
        (invite_id, inviter_peer_shared_id, address, port,
         inviter_transit_prekey_id, inviter_transit_prekey_public_key,
         created_at, recorded_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (event['invite_id'], invite_data['inviter_peer_shared_id'],
          inviter_address, inviter_port, inviter_transit_prekey_id,
          inviter_transit_prekey_public_key,
          event['created_at'], event['recorded_by']))
```

**Update `send_connect_to_all()`:**

```python
def send_connect_to_all(db):
    # Existing: send to all peers_shared addresses
    # ... existing code ...

    # NEW: Also send to inviter addresses
    invite_accepteds = db.execute("""
        SELECT inviter_peer_shared_id, address, port,
               inviter_transit_prekey_id, inviter_transit_prekey_public_key
        FROM invite_accepteds
        WHERE address IS NOT NULL AND port IS NOT NULL
    """).fetchall()

    for invite in invite_accepteds:
        # Send sync_connect to inviter address:port
        # Sealed to inviter's transit_prekey
```

**Implementation Steps:**

1. Create `events/identity/invite_accepted.sql`:
   - Define `invite_accepteds` table schema

2. Update `events/identity/invite_accepted.py`:
   - Extract inviter metadata from invite blob
   - Insert into `invite_accepteds` table
   - Keep existing behavior (store invite_private_key in group_prekeys)

3. Update `events/transit/sync_connect.py`:
   - Query `invite_accepteds` for connection targets
   - Send connects to inviter addresses

**Files to Modify:**
- NEW: `events/identity/invite_accepted.sql` (table schema)
- `events/identity/invite_accepted.py` (project to invite_accepteds)
- `events/transit/sync_connect.py` (query invite_accepteds)

### 3.3 Remove Bootstrap Send Logic

**Goal**: Delete all bootstrap-specific code and use normal sync for everything.

**Code to Delete:**

1. **Function**: `send_bootstrap_to_peer()` (sync.py:582-693)
   - Entire function can be deleted
   - Normal `send_response()` handles everything

2. **Table**: `bootstrap_completers`
   - No longer needed
   - Delete table and all references

3. **Table**: `network_joiners`
   - No longer needed for bootstrap mode tracking
   - Delete table and all references

4. **Bootstrap mode checks** in `send_request_to_all()`:
   ```python
   # DELETE this:
   my_bootstrap_complete = is_bootstrap_complete(recorded_by, db)
   if not my_bootstrap_complete:
       # special bootstrap logic
   ```

**Keep (for metadata only):**
- `network_joined` event - still useful to mark "I joined this network"
- `network_created` event - still useful to mark "I created this network"
- These become pure metadata, not used for control flow

**Implementation Steps:**

1. Delete `events/transit/sync.py:582-693`:
   - Remove `send_bootstrap_to_peer()` function

2. Delete `events/identity/bootstrap_complete.py`:
   - Remove entire file
   - Remove from imports

3. Update `events/transit/sync.py:send_request_to_all()`:
   - Remove bootstrap mode checks
   - Always use normal bloom-filtered sync

4. Update `events/identity/network_joined.py`:
   - Keep event for metadata
   - Remove projection to `network_joiners` table
   - Just record the fact of joining

5. Drop tables:
   ```sql
   DROP TABLE IF EXISTS bootstrap_completers;
   DROP TABLE IF EXISTS network_joiners;
   ```

**Files to Modify:**
- `events/transit/sync.py` (delete send_bootstrap_to_peer)
- DELETE: `events/identity/bootstrap_complete.py`
- `events/identity/network_joined.py` (simplify projection)
- `events/transit/sync.sql` (drop bootstrap tables)

### 3.4 Fix Invite Prekey Routing

**Goal**: Make routing and decryption work for blobs sealed to invite prekeys.

**Current Bug** (from BOOTSTRAP_PREKEY_FIX.md):
- Bootstrap blobs wrapped with invite_prekey
- `route_blob_to_peers()` only checks `transit_keys` and `transit_prekeys_shared`
- Doesn't check `group_prekeys` where invite_private_key is stored
- Result: bootstrap blobs don't route/decrypt correctly

**Fix routing:**

```python
def route_blob_to_peers(hint, db):
    """Returns list of peer IDs who can decrypt this blob."""

    # Existing: check transit_keys (symmetric)
    symmetric_peers = db.execute("""
        SELECT recorded_by FROM transit_keys
        WHERE transit_key_id = ?
    """, (hint,)).fetchall()

    # Existing: check transit_prekeys (asymmetric)
    asymmetric_peers = db.execute("""
        SELECT recorded_by FROM transit_prekeys_shared
        WHERE transit_prekey_id = ?
    """, (hint,)).fetchall()

    # NEW: check group_prekeys (invite prekeys)
    invite_peers = db.execute("""
        SELECT recorded_by FROM group_prekeys
        WHERE prekey_id = ?
    """, (hint,)).fetchall()

    return symmetric_peers + asymmetric_peers + invite_peers
```

**Fix decryption:**

```python
def unwrap_transit(blob, hint, db):
    """Decrypt transit-layer encryption."""

    # Try symmetric first
    symmetric_key = get_transit_key(hint, db)
    if symmetric_key:
        return decrypt_symmetric(blob, symmetric_key)

    # Try asymmetric transit prekey
    asymmetric_key = get_transit_prekey(hint, db)
    if asymmetric_key:
        return decrypt_asymmetric(blob, asymmetric_key)

    # NEW: Try group prekey (for invite prekeys)
    group_prekey = get_group_prekey(hint, db)
    if group_prekey:
        return decrypt_asymmetric(blob, group_prekey)

    return None  # Can't decrypt
```

**Implementation Steps:**

1. Update `events/transit/sync.py:route_blob_to_peers()`:
   - Add query for `group_prekeys` table
   - Include those peers in routing result

2. Update `core/crypto.py:unwrap_transit()`:
   - Add fallback to check `group_prekeys` table
   - Use same asymmetric decrypt logic as transit_prekeys

3. Add test for invite prekey routing:
   - Create invite with prekey
   - Send blob wrapped to invite prekey
   - Verify joiner can decrypt

**Files to Modify:**
- `events/transit/sync.py:235-271` (routing)
- `core/crypto.py` (unwrap_transit, if separate function)

### Phase 3 Success Criteria
- [ ] Sync requests support invite authentication
- [ ] `invite_accepteds` table stores inviter connection info
- [ ] Bootstrap send logic completely removed
- [ ] `bootstrap_completers` and `network_joiners` tables deleted
- [ ] Invite prekey routing works correctly
- [ ] Joiner can sync with normal requests (no bootstrap)
- [ ] All scenario tests pass without bootstrap

---

## Phase 4: Simplify Join Event Blocking

**Goal**: Make joiner-side blocking consistent - block join events until dependencies sync.

### Rationale

Currently, there are special cases to skip validation during bootstrap:
- `user.project()` skips invite validation if bootstrap (line 292)
- `invite.project()` skips validation if bootstrap (line 288)
- "Foreign local deps" pattern skips validation of creator's local data

This complexity exists to break circular dependencies during bootstrap. With normal sync and proper dependency ordering, we can:
- Remove all validation skips
- Let dependency blocking handle everything naturally
- Treat all peers the same (no special bootstrap path)

### 4.1 Remove Bootstrap Validation Skip

**Current code** (user.py:286-298):

```python
def project(event, db):
    # ... validation logic ...

    # SPECIAL CASE: Skip validation during bootstrap
    is_bootstrap = check_if_bootstrap(recorded_by, db)
    if is_bootstrap:
        # Skip invite validation
        pass
    else:
        # Validate invite_proof
        validate_invite_proof(event, db)
```

**New code** (simplified):

```python
def project(event, db):
    # ... validation logic ...

    # NO special cases - always validate
    # Dependencies will block until invite is available
    validate_invite_proof(event, db)
```

**Implementation Steps:**

1. Update `events/identity/user.py:project()`:
   - Remove `is_bootstrap` check (line 292)
   - Remove conditional validation skip
   - Always run full validation

2. Update `events/identity/invite.py:project()`:
   - Remove similar bootstrap skip (line 288)
   - Always validate invite

3. Update `events/identity/link.py:project()`:
   - Remove any bootstrap-related skips
   - Always validate

4. Ensure dependencies are correct:
   - `user` depends on `invite_id`
   - `invite_proof` depends on `invite_id` and `user_id`
   - Blocking system will naturally wait for dependencies

**Files to Modify:**
- `events/identity/user.py:286-298` (remove bootstrap skip)
- `events/identity/invite.py:~288` (remove bootstrap skip)
- `events/identity/link.py` (remove any bootstrap skips)

### 4.2 Remove Foreign Local Deps Pattern

**Current code** (recorded.py:67-100):

```python
def project(event, db):
    # ... validation ...

    # SPECIAL CASE: Skip deps that reference creator's local data
    if event['created_by'] != event['recorded_by']:
        # This is a "foreign" event
        skip_deps = []
        for dep_id in event['deps']:
            dep_type = get_event_type(dep_id, db)
            if dep_type in LOCAL_CREATOR_TYPES:
                # Skip this dependency (can't validate foreign local data)
                skip_deps.append(dep_id)

        event['deps'] = [d for d in event['deps'] if d not in skip_deps]
```

**New approach**:

1. **Project local-only events first**:
   - Process `peer`, `transit_prekey`, `group_prekey`, etc. BEFORE shareable events
   - Local events are never dependencies (don't add to `deps` list)
   - This makes them available for projection but not required for validation

2. **Remove LOCAL_CREATOR_TYPES concept**:
   - No special handling for foreign perspectives
   - All shareable events validate the same way for everyone

**Implementation Steps:**

1. Update pipeline order (in `core/pipeline.py` or wherever event processing is orchestrated):
   ```python
   # Process local-only events first
   local_events = [e for e in events if e['type'] in LOCAL_ONLY_TYPES]
   shareable_events = [e for e in events if e['type'] not in LOCAL_ONLY_TYPES]

   for event in local_events:
       project(event, db)

   for event in shareable_events:
       project(event, db)  # Can reference local events, but they're not deps
   ```

2. Update event creation to NOT include local events in deps:
   ```python
   # When creating group_key_shared:
   deps = [group_id, channel_id, key_id]
   # Do NOT include group_prekey_id as a dep (it's local-only)
   ```

3. Delete foreign local deps logic from `events/transit/recorded.py:67-100`:
   - Remove special case handling
   - Simplify projection logic

4. Update event types to mark local-only clearly:
   ```python
   LOCAL_ONLY_TYPES = {
       'peer',
       'transit_prekey',
       'group_prekey',
       'transit_key',
       'group_key',
       'invite_accepted',
       'link_invite_accepted',
   }
   ```

**Files to Modify:**
- `events/transit/recorded.py:67-100` (delete foreign deps logic)
- `core/pipeline.py` or event processing (order local events first)
- Various event `create()` functions (don't include local events in deps)

### 4.3 Update Event Dependencies

**Goal**: Ensure dependency chains are correct for natural blocking.

**Current dependencies:**

```python
# user event
deps = [invite_id, peer_shared_id]

# invite_proof event (new in Phase 2)
deps = [invite_id, user_id]  # or link_user_id

# group_key_shared event
deps = [group_id, key_id, prekey_id]  # prekey_id is local-only!
```

**New dependencies:**

```python
# user event
deps = [invite_id]  # peer_shared_id is local, not a dep

# invite_proof event
deps = [invite_id, user_id]  # blocks until both available

# group_key_shared event
deps = [group_id, key_id]  # prekey_id is local, not a dep
```

**Rules:**
1. Only include shareable events in `deps`
2. Local-only events (peer, prekey, key) are NOT deps
3. Events block naturally until all deps are validated
4. No special cases for bootstrap or foreign perspectives

**Implementation Steps:**

1. Audit all event `create()` functions:
   - Check what's included in `deps`
   - Remove local-only events from deps

2. Update specific events:
   - `user.create()` - remove peer_shared_id from deps
   - `link.create()` - remove peer_shared_id from deps
   - `group_key_shared.create()` - remove prekey_id from deps
   - Any others that reference local events

3. Verify blocking works:
   - Events that reference unavailable deps go to blocked table
   - Events unblock when deps become available
   - No special unblocking logic needed

**Files to Modify:**
- `events/identity/user.py` (update deps)
- `events/identity/link.py` (update deps)
- `events/encryption/group_key_shared.py` (update deps)
- Others as needed

### Phase 4 Success Criteria
- [ ] No bootstrap validation skips in user/invite/link projection
- [ ] Foreign local deps pattern completely removed
- [ ] Local-only events processed first, not included in deps
- [ ] Event dependencies only reference shareable events
- [ ] Natural blocking handles all dependency resolution
- [ ] All scenario tests pass with simplified blocking

---

## Phase 5: Align Network Creation with Join Flow

**Goal**: Make network creator use same join flow as subsequent users.

### Rationale

Currently, `user.new_network()` has a completely different code path than `user.join()`:
- Creates groups, network, channel directly
- Creates `network_created` event to mark self-bootstrapped
- No invite or invite_proof involved

The ideal protocol design says the first user should join using an invite link, just like everyone else. This eliminates the special case and makes the creator flow consistent.

### 5.1 Creator Self-Invite

**Current flow** (user.new_network):

```python
def new_network(name, ...):
    # Create peer
    peer_id = peer.create(...)

    # Create groups
    all_users_group_id = group.create(...)
    admins_group_id = group.create(...)

    # Create network
    network_id = network.create(...)

    # Create channel
    channel_id = channel.create(...)

    # Create user (creator)
    user_id = user.create(name=name, ...)

    # Create network_created event
    network_created.create(...)

    return user_id
```

**New flow** (creator uses invite):

```python
def new_network(name, ...):
    # Create peer
    peer_id = peer.create(...)

    # Create invite for self (mode='user')
    invite_id, invite_link = invite.create(
        mode='user',
        # ... prekeys, groups, etc.
    )

    # Join using own invite link (same as any joiner!)
    user_id = user.join(
        invite_link=invite_link,
        name=name,
        ...
    )

    return user_id
```

**Implementation approach:**

1. **Create bootstrap invite**:
   - Creator generates invite event before joining
   - Invite references groups/channel/keys that don't exist yet
   - These will be blocked until creator syncs them

2. **Join using own invite**:
   - Call `user.join()` with self-created invite link
   - Same code path as any other joiner
   - Creates user + invite_proof events

3. **Sync with self**:
   - Creator "syncs" with themselves to unblock dependencies
   - Or: special case to project own events immediately
   - Or: mark certain events as "valid by default" during creation

**Challenges:**

- **Circular deps**: invite references groups, groups reference user, user references invite
- **Self-sync**: How does creator get their own events?

**Solutions:**

Option A: **Creator projects immediately**
```python
def new_network(name, ...):
    # Create and immediately project all bootstrap events
    all_users_group_id = group.create_and_project(...)
    admins_group_id = group.create_and_project(...)
    network_id = network.create_and_project(...)
    channel_id = channel.create_and_project(...)

    # Create invite referencing projected data
    invite_id, invite_link = invite.create(
        all_users_group_id=all_users_group_id,
        ...
    )

    # Join normally (invite deps already satisfied)
    user_id = user.join(invite_link=invite_link, name=name, ...)
```

Option B: **Bootstrap flag for self-created events**
```python
# Mark events as "created by me, valid by default"
def project(event, db):
    if event['created_by'] == event['recorded_by']:
        # Self-created event, skip some validation
        if not all_deps_available(event, db):
            # Still validate, but more permissive
            pass
```

Option C: **Keep network_created special case**
```python
# Keep user.new_network() mostly as-is
# Just make it create an invite_proof for consistency
def new_network(name, ...):
    # Existing creation logic...

    # Create invite for metadata (even though not used)
    invite_id = invite.create(...)

    # Create invite_proof for consistency
    invite_proof.create(
        invite_id=invite_id,
        mode='user',
        user_id=user_id,
        ...
    )
```

**Recommended**: Option C (keep special case, add invite_proof)
- Least disruptive
- Still adds consistency (invite_proof exists for creator)
- Can be fully unified in future if needed

### 5.2 Update Tests

**Implementation Steps:**

1. Update `events/identity/user.py:new_network()`:
   - Add invite creation
   - Add invite_proof creation
   - Keep existing group/network/channel creation logic

2. Optionally simplify later:
   - Move toward Option A (immediate projection) if desired
   - Or fully unify with Option B (permissive self validation)

3. Update tests:
   - Verify creator still becomes admin
   - Verify invite_proof exists for creator
   - Verify creator can invite others

**Files to Modify:**
- `events/identity/user.py:new_network()` (add invite/invite_proof)
- Tests (verify invite_proof for creator)

### Phase 5 Success Criteria
- [ ] Network creator creates invite_proof (even if special case remains)
- [ ] Creator admin privileges work correctly
- [ ] Creator can invite other users
- [ ] All tests pass with creator invite_proof

---

## Testing Strategy

### Test Pyramid

**Unit Tests** (test individual components):
- New event types (invite_proof, sync_connect)
- New validation logic
- New projection logic
- Dependency resolution

**Integration Tests** (test event flows):
- Join flow end-to-end
- Link flow end-to-end
- Sync flow with connections
- Invite authentication

**Scenario Tests** (test realistic multi-peer scenarios):
- Three-way messaging (Alice creates, Bob joins, Charlie joins)
- Multi-device linking (Alice links phone to laptop)
- Message expiry with TTL
- Network creation (creator flow)

### Test Updates Per Phase

**Phase 1 (Connect-Then-Sync):**
- NEW: `test_sync_connect.py` - connection establishment
- MODIFY: `test_three_player_messaging.py` - add connect phase
- MODIFY: `test_multi_device_linking.py` - add connect phase

**Phase 2 (Unified Invite):**
- NEW: `test_invite_proof.py` - standalone proof validation
- MODIFY: `test_user_join.py` - verify separate invite_proof
- MODIFY: `test_link_join.py` - verify separate invite_proof

**Phase 3 (Remove Bootstrap):**
- DELETE: Any bootstrap-specific tests
- MODIFY: All join tests - verify sync works without bootstrap
- NEW: `test_invite_prekey_routing.py` - verify routing fix

**Phase 4 (Simplify Blocking):**
- MODIFY: Dependency tests - verify natural blocking
- DELETE: Foreign local deps tests
- VERIFY: All events block/unblock correctly

**Phase 5 (Creator Self-Invite):**
- MODIFY: `test_new_network.py` - verify creator invite_proof
- VERIFY: Creator admin privileges work

### Regression Testing

After each phase:
1. Run ALL existing tests
2. Fix any failures
3. Update tests to match new patterns
4. Add new tests for new features

**Key test files:**
- `tests/scenario_tests/test_three_player_messaging.py`
- `tests/scenario_tests/test_multi_device_linking.py`
- `tests/scenario_tests/test_message_expiry.py`
- `tests/events/test_user.py`
- `tests/events/test_link.py`

---

## Migration Path

### Backward Compatibility

**During refactoring:**
- Keep old event types working alongside new ones
- Support both bootstrap and sync-with-invite modes
- Allow both embedded and standalone invite_proof

**Version flags:**
```python
# In database schema
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    description TEXT,
    applied_at INTEGER
);

# Check version before using new features
if schema_version >= 2:
    # Use invite_proof
else:
    # Use embedded proof in user event
```

### Migration Scripts

**If needed** (for existing databases):

```python
def migrate_to_unified_invites(db):
    """Convert link_invites to invites with mode='link'."""

    # Copy link_invites to invites
    db.execute("""
        INSERT INTO invites (invite_id, mode, user_id, ...)
        SELECT link_invite_id, 'link', user_id, ...
        FROM link_invites
    """)

    # Update references
    # ... (if any stored invite_ids need updating)

    # Increment schema version
    db.execute("INSERT INTO schema_version VALUES (2, 'unified_invites', ?)", (t_ms,))
```

### Rollback Plan

**If issues arise:**

1. **Phase 1**: Revert to old sync (keep bootstrap)
   - Restore `send_bootstrap_to_peer()`
   - Remove `sync_connect`

2. **Phase 2**: Keep separate invite types
   - Revert `invite_proof` changes
   - Keep invite/link_invite separate

3. **Phase 3**: Restore bootstrap
   - Add back bootstrap send logic
   - Restore bootstrap tables

4. **Phase 4**: Keep validation skips
   - Restore bootstrap validation skips
   - Keep foreign local deps pattern

5. **Phase 5**: Keep creator special case
   - Keep `new_network()` as-is
   - Don't require invite_proof for creator

---

## Success Criteria (Summary)

### Phase 1: Connect-Then-Sync
- [ ] `sync_connect` event type created and tested
- [ ] `sync_connections` table tracks active connections
- [ ] Sync uses connections instead of direct prekey targeting
- [ ] Reflected sync establishes two-way communication
- [ ] All existing scenario tests pass

### Phase 2: Unified Invite
- [ ] `invite` event supports both mode='user' and mode='link'
- [ ] `invite_proof` is standalone event, validates consistently
- [ ] `user.join()` and `link.join()` create separate invite_proof
- [ ] Backward compatibility maintained
- [ ] All scenario tests pass

### Phase 3: Remove Bootstrap
- [ ] Sync requests support invite authentication
- [ ] `invite_accepteds` table stores inviter connection info
- [ ] Bootstrap send logic completely removed
- [ ] Invite prekey routing works correctly
- [ ] All scenario tests pass without bootstrap

### Phase 4: Simplify Blocking
- [ ] No bootstrap validation skips
- [ ] Foreign local deps pattern removed
- [ ] Local-only events processed first
- [ ] Natural blocking handles all dependencies
- [ ] All scenario tests pass

### Phase 5: Creator Self-Invite
- [ ] Network creator creates invite_proof
- [ ] Creator admin privileges work
- [ ] All tests pass

### Overall Success
- [ ] Codebase is simpler (fewer special cases)
- [ ] All scenario tests pass
- [ ] No regression in functionality
- [ ] Protocol aligns with ideal design
- [ ] Ready for next features (forward secrecy, etc.)

---

## Timeline Estimate

**Phase 1: Connect-Then-Sync** - 3-5 days
- 1 day: Implement sync_connect event
- 1 day: Update sync to use connections
- 1-2 days: Update tests and verify
- 1 day: Buffer for issues

**Phase 2: Unified Invite** - 4-6 days
- 1 day: Extend invite schema
- 2 days: Create invite_proof event type
- 1 day: Update join flows
- 1-2 days: Update tests and verify

**Phase 3: Remove Bootstrap** - 3-5 days
- 1 day: Add invite auth to sync
- 1 day: Create invite_accepteds table
- 1 day: Remove bootstrap code
- 1 day: Fix invite prekey routing
- 1 day: Update tests and verify

**Phase 4: Simplify Blocking** - 2-4 days
- 1 day: Remove validation skips
- 1 day: Remove foreign local deps
- 1-2 days: Update tests and verify

**Phase 5: Creator Self-Invite** - 2-3 days
- 1 day: Update new_network()
- 1 day: Update tests and verify
- 1 day: Buffer for issues

**Total: 14-23 days** (3-5 weeks)

---

## Next Steps

1. **Review this plan** - confirm approach with team/self
2. **Set up branch** - create feature branch for refactoring
3. **Start Phase 1** - implement connect-then-sync pattern
4. **Iterate** - complete each phase, test thoroughly
5. **Merge** - integrate into main branch when complete

## Questions / Decisions Needed

1. **Option for creator self-invite** (Phase 5):
   - Option A: Immediate projection
   - Option B: Permissive self validation
   - Option C: Keep special case, add invite_proof (recommended)

2. **Migration strategy**:
   - Support both old and new formats during transition?
   - Provide migration scripts for existing databases?
   - Timeline for deprecating old formats?

3. **Testing approach**:
   - Run tests after each phase or at the end?
   - Update tests incrementally or all at once?
   - How much backward compatibility to maintain?

---

## References

- `ideal_protocol_design.md` - Target protocol design
- `joining_linking_simplification.md` - Detailed refactoring proposal
- `BOOTSTRAP_PREKEY_FIX.md` - Bootstrap routing bug documentation
- Current implementation:
  - `events/identity/user.py` - User join flow
  - `events/identity/link.py` - Device linking flow
  - `events/transit/sync.py` - Sync protocol
