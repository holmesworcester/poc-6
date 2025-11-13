# Multi-Device Linking Implementation Plan

## Overview

Implement multi-device linking using the unified invite primitive design from `ideal_protocol_design.md`, building on the existing Phase 5 abstraction (first_peer self-invite pattern) and the recently implemented `invite_proof` event system.

## Current State

### What's Already Built

1. **Unified Invite Primitive (Phase 2 - Partially Complete)**
   - ✅ `invite` event supports `mode` field ('user' or 'link')
   - ✅ Separate `invite_proof` event validates invite possession
   - ✅ `invite_proof.project()` conditionally handles mode='user' and mode='link'
   - ⚠️ `link_invite.py` still exists as separate module (to be consolidated)

2. **First Peer Bootstrap (Phase 5 - Complete)**
   - ✅ Network creator uses self-invite with `first_peer` field
   - ✅ `is_admin()` function checks both normal path and first_peer path
   - ✅ Admin privileges granted via event (syncs to all peers)
   - ✅ 25/32 scenario tests passing

3. **Invite Proof Architecture**
   - ✅ `invite_proof.py` module with standalone event
   - ✅ Validates Ed25519 signature proving invite_private_key possession
   - ✅ For mode='user': Adds to group_members table
   - ✅ For mode='link': Link event handles linked_peers insertion
   - ✅ Dependency ordering: invite → user/link → invite_proof

### What Needs to Be Built

1. **Complete unified invite for linking** (Phase 2.4 continuation)
   - Consolidate `link_invite.create()` into `invite.create()` with mode='link'
   - Ensure ALL group keys are shared when mode='link'
   - Remove separate link_invite module after migration

2. **Implement link.join() flow**
   - Create `link.join()` that uses unified invite system
   - Generate `invite_proof` event with mode='link'
   - Update `link.project()` to work with new flow

3. **Handle group membership inheritance**
   - Ensure linked devices inherit all group memberships
   - Test groups added BEFORE invite creation
   - Test groups added BETWEEN invite creation and acceptance

4. **Comprehensive scenario tests**
   - Test pre-existing group visibility
   - Test new group visibility
   - Test bidirectional messaging
   - Test admin privilege inheritance

## Design Principles

### Event Flow for Device Linking

NOTE: All devices are equal - there is no primary/master device, just a first device and subsequent devices.

```
Existing Device (Alice's laptop):
1. invite.create(mode='link', user_id=alice_user_id)
   → Creates invite event
   → Shares ALL group keys sealed to invite_prekey
   → Returns invite_link with invite_private_key

Additional Device (Alice's phone):
2. link.join(invite_link)
   → Creates link event with link_user_id=alice_user_id
   → Creates invite_proof event with mode='link', signature
   → Creates peer and transit_prekey_shared for sync

Projection (both devices):
3. link.project()
   → Validates link event structure
   → Inserts into linked_peers table
   → Creates users table entry (same user_id, new peer_id)

4. invite_proof.project()
   → Validates Ed25519 signature
   → For mode='link': Does nothing (link.project handled it)
   → Unblocks group_key_shared events

Result:
   → Additional device has same user_id, different peer_id
   → Inherits all group memberships via users table
   → Can decrypt all groups via group_key_shared events
   → Both devices are equal - no primary/secondary distinction
```

### Group Key Sharing Strategy

**For mode='user' (joining):**
- Share `all_users` group key
- Share `admins` group key
- New user added to all_users group via invite_proof.project()

**For mode='link' (device linking):**
- Share ALL group keys (iterate through user's group memberships)
- New device inherits all memberships automatically (same user_id)
- No need to add to group_members (already member via user_id)

### Group Visibility Scenarios

**Scenario 1: Groups Before Invite**
1. Alice creates groups A, B, C
2. Alice creates link invite
   → Shares keys for groups A, B, C via group_key_shared events
3. New device joins via link
   → Unseals group_key_shared events
   → Can decrypt all groups

**Scenario 2: Groups Between Invite and Join**
1. Existing device creates link invite
   → Shares keys for groups A, B (existing at time)
2. Existing device user joins group C (after invite, before additional device joins)
   → No group_key_shared for C sealed to invite_prekey
3. Additional device joins via link
   → Can decrypt A, B immediately
   → Cannot decrypt C until key explicitly shared
   → **SOLUTION**: After link acceptance, share keys for new groups via normal group_key_shared (sealed to additional device's prekey)

**Scenario 3: Groups After Join**
1. User links additional device successfully
2. User (via any device) joins group D
   → Shares key to all user's devices via group_key_shared
3. All devices receive group_key_shared in sync
   → Can decrypt group D

## Implementation Steps

### Step 1: Setup Worktree Branch
- Create worktree branch `multi-device-linking` based on master
- Ensure clean starting point for development

### Step 2: Consolidate link_invite into invite (Phase 2.4)

**File: events/identity/invite.py**

Modify `invite.create()` to handle mode='link':

```python
def create(
    peer_id: str,
    mode: str = 'user',  # 'user' or 'link'
    user_id: str | None = None,  # Required when mode='link'
    t_ms: int,
    db: Database
) -> tuple[str, str, dict]:
    """
    Creates an invite event for either user joining or device linking.

    For mode='user': Creates invite for new user to join network
    For mode='link': Creates invite for existing user to add new device

    Returns: (invite_id, invite_link, invite_data)
    """

    # Validate parameters
    if mode == 'link' and user_id is None:
        raise ValueError("user_id required when mode='link'")

    # Generate invite keypair (same for both modes)
    invite_private_key, invite_pubkey = generate_ed25519_keypair()

    # Create invite event
    invite_event = {
        'mode': mode,
        'invite_pubkey': invite_pubkey,
    }

    if mode == 'link':
        invite_event['user_id'] = user_id

    # Record invite event
    invite_id = recorded.create(
        event_type='invite',
        peer_id=peer_id,
        event_json=invite_event,
        t_ms=t_ms,
        db=db
    )

    # Share group keys
    if mode == 'user':
        # Share all_users and admins group keys
        _share_user_invite_keys(invite_id, invite_pubkey, peer_id, t_ms, db)
    else:  # mode='link'
        # Share ALL group keys for this user
        _share_link_invite_keys(invite_id, invite_pubkey, user_id, peer_id, t_ms, db)

    # Generate invite link (same format for both modes)
    invite_link = _encode_invite_link(invite_id, invite_private_key, peer_id, db)

    return invite_id, invite_link, {...}
```

**New helper function:**
```python
def _share_link_invite_keys(
    invite_id: str,
    invite_pubkey: bytes,
    user_id: str,
    peer_id: str,
    t_ms: int,
    db: Database
):
    """
    Shares ALL group keys for a user's existing memberships.
    Used when creating link invites for device linking.
    """
    # Query all groups this user is a member of
    rows = db.execute("""
        SELECT DISTINCT gm.group_id
        FROM group_members gm
        WHERE gm.user_id = ?
    """, (user_id,))

    for row in rows:
        group_id = row[0]

        # Share this group's key sealed to invite_pubkey
        group_key_shared.create(
            peer_id=peer_id,
            group_id=group_id,
            recipient_prekey=invite_pubkey,
            t_ms=t_ms,
            db=db
        )
```

**Keep link_invite.py temporarily** for backward compatibility, but have it call `invite.create(mode='link')` internally.

### Step 3: Implement link.join() Using Unified Invite

**File: events/identity/link.py**

Create new `link.join()` method:

```python
def join(
    peer_id: str,
    invite_link: str,
    t_ms: int,
    db: Database
) -> dict:
    """
    Joins an existing user's network by adding a new device.

    Uses unified invite system with mode='link'.

    Returns: {'user_id': ..., 'peer_id': ...}
    """
    # Decode invite link
    invite_data = _decode_invite_link(invite_link)
    invite_id = invite_data['invite_id']
    invite_private_key = invite_data['invite_private_key']
    inviter_peer_id = invite_data['peer_id']

    # Project inviter's peer_shared event (needed for sync)
    peer.project_from_link(invite_data['peer_shared_event'], db)

    # Store invite_private_key for unsealing group_key_shared events
    invite_accepted.create(
        peer_id=peer_id,
        invite_id=invite_id,
        invite_private_key=invite_private_key,
        t_ms=t_ms,
        db=db
    )

    # Fetch invite to get user_id
    invite_event = db.execute("""
        SELECT event_json FROM valid_events
        WHERE event_id = ? AND event_type = 'invite'
    """, (invite_id,)).fetchone()

    if not invite_event:
        raise ValueError(f"Invite {invite_id} not found")

    invite_json = json.loads(invite_event[0])
    link_user_id = invite_json.get('user_id')

    if not link_user_id:
        raise ValueError("Invite missing user_id (not a link invite)")

    # Create link event
    link_event_id = recorded.create(
        event_type='link',
        peer_id=peer_id,
        event_json={
            'invite_id': invite_id,
            'link_user_id': link_user_id,
        },
        t_ms=t_ms,
        db=db
    )

    # Create invite_proof event with mode='link'
    invite_proof_id = invite_proof.create(
        peer_id=peer_id,
        invite_id=invite_id,
        invite_private_key=invite_private_key,
        mode='link',
        link_user_id=link_user_id,
        t_ms=t_ms,
        db=db
    )

    # Create peer_shared and transit_prekey_shared for receiving sync
    peer_shared.create(peer_id=peer_id, t_ms=t_ms, db=db)
    transit_prekey_shared.create(peer_id=peer_id, t_ms=t_ms, db=db)

    return {
        'user_id': link_user_id,
        'peer_id': peer_id,
        'link_event_id': link_event_id,
        'invite_proof_id': invite_proof_id,
    }
```

### Step 4: Update link.project() for New Flow

**File: events/identity/link.py**

Ensure `link.project()` works with the new invite_proof system:

```python
def project(event: dict, db: Database):
    """
    Projects a link event, adding a new device to an existing user.

    Dependencies: invite must exist and be valid
    Effects: Inserts into linked_peers and users tables
    """
    link_event_id = event['event_id']
    peer_id = event['peer_id']
    invite_id = event['event_json']['invite_id']
    link_user_id = event['event_json']['link_user_id']

    # Validate peer signature
    if not _validate_peer_signature(event, db):
        raise ValueError("Invalid peer signature")

    # Fetch invite to verify it exists and has mode='link'
    invite_event = db.execute("""
        SELECT event_json FROM valid_events
        WHERE event_id = ? AND event_type = 'invite'
    """, (invite_id,)).fetchone()

    if not invite_event:
        raise ValueError(f"Invite {invite_id} not found")

    invite_json = json.loads(invite_event[0])

    if invite_json.get('mode') != 'link':
        raise ValueError("Invite is not a link invite")

    if invite_json.get('user_id') != link_user_id:
        raise ValueError("Link user_id doesn't match invite")

    # Insert into linked_peers table
    db.execute("""
        INSERT INTO linked_peers (user_id, peer_id, link_event_id)
        VALUES (?, ?, ?)
        ON CONFLICT DO NOTHING
    """, (link_user_id, peer_id, link_event_id))

    # Insert into users table (same user_id, different peer_id)
    db.execute("""
        INSERT INTO users (user_id, peer_id)
        VALUES (?, ?)
        ON CONFLICT DO NOTHING
    """, (link_user_id, peer_id))

    # Note: invite_proof.project() will validate the invite_private_key signature
    # No need to add to group_members - user already has memberships via user_id
```

### Step 5: Verify invite_proof.project() Handles mode='link'

**File: events/identity/invite_proof.py**

Confirm existing logic properly handles mode='link' (should already be correct):

```python
def project(event: dict, db: Database):
    """
    Projects an invite_proof event, validating invite possession.

    For mode='user': Adds user to group_members
    For mode='link': No additional action (link.project handled users table)
    """
    # ... validation logic ...

    if event['mode'] == 'user':
        # Add to group_members for the invite's group_id
        user_id = event['user_id']
        group_id = invite_json['group_id']  # from invite metadata

        db.execute("""
            INSERT INTO group_members (user_id, group_id, event_id)
            VALUES (?, ?, ?)
            ON CONFLICT DO NOTHING
        """, (user_id, group_id, event['event_id']))

    else:  # mode='link'
        # Link event already handled linked_peers and users tables
        # User already has group memberships via existing user_id
        pass
```

### Step 6: Handle Groups Added Between Invite Creation and Join

This is the tricky scenario: Alice creates link invite, then creates a new group, then the new device joins.

**Challenge**: The invite was created before the group existed, so no group_key_shared event was sealed to the invite_prekey.

**Solution**: After link acceptance, explicitly share keys for any new groups.

**Implementation: Add post-join key sharing to link.join()**

```python
def join(peer_id, invite_link, t_ms, db):
    # ... existing join logic ...

    # After link event is created, check for new groups and share keys
    _share_new_group_keys_after_link(
        linked_user_id=link_user_id,
        inviter_peer_id=inviter_peer_id,
        new_device_peer_id=peer_id,
        t_ms=t_ms,
        db=db
    )

    return {...}

def _share_new_group_keys_after_link(
    linked_user_id: str,
    inviter_peer_id: str,
    new_device_peer_id: str,
    t_ms: int,
    db: Database
):
    """
    Shares keys for groups that were added after invite creation.

    This handles the scenario where:
    1. User creates link invite
    2. User joins new group
    3. New device accepts link invite

    Without this, the new device wouldn't have keys for groups
    joined between invite creation and acceptance.
    """
    # Get new device's transit prekey
    prekey_row = db.execute("""
        SELECT prekey FROM transit_prekeys
        WHERE peer_id = ?
        ORDER BY created_at DESC
        LIMIT 1
    """, (new_device_peer_id,)).fetchone()

    if not prekey_row:
        # New device hasn't shared prekey yet, keys will sync later
        return

    recipient_prekey = prekey_row[0]

    # Get all groups the user is a member of
    group_rows = db.execute("""
        SELECT DISTINCT group_id
        FROM group_members
        WHERE user_id = ?
    """, (linked_user_id,))

    for row in group_rows:
        group_id = row[0]

        # Share key from inviter's perspective (they have the key)
        group_key_shared.create(
            peer_id=inviter_peer_id,  # Inviter shares key
            group_id=group_id,
            recipient_prekey=recipient_prekey,
            t_ms=t_ms,
            db=db
        )
```

### Step 7: Create Comprehensive Scenario Tests

**Test 1: test_link_device_pre_existing_groups.py**

Tests that groups created BEFORE link invite are visible to linked device.

```python
def test_link_device_pre_existing_groups():
    """
    Scenario:
    1. Alice creates network and becomes admin (first_peer)
    2. Alice creates groups A, B, C
    3. Alice creates link invite
    4. Alice's phone joins via link
    5. Verify phone can see groups A, B, C
    """
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Step 1: Alice creates network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Step 2: Create groups A, B, C
    group_a_id = group.create(
        peer_id=alice['peer_id'],
        group_name='Group A',
        t_ms=2000,
        db=db
    )
    group_b_id = group.create(
        peer_id=alice['peer_id'],
        group_name='Group B',
        t_ms=2500,
        db=db
    )
    group_c_id = group.create(
        peer_id=alice['peer_id'],
        group_name='Group C',
        t_ms=3000,
        db=db
    )

    # Sync to ensure groups are fully created
    for i in range(5):
        tick.tick(t_ms=3500 + i*100, db=db)

    # Step 3: Alice creates link invite
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        mode='link',
        user_id=alice['user_id'],
        t_ms=4000,
        db=db
    )

    # Step 4: Alice's phone joins via link
    alice_phone_peer_id, _ = peer.create(t_ms=5000, db=db)
    alice_phone = link.join(
        peer_id=alice_phone_peer_id,
        invite_link=invite_link,
        t_ms=5000,
        db=db
    )

    # Step 5: Sync multiple rounds for convergence
    for i in range(10):
        tick.tick(t_ms=6000 + i*200, db=db)

    # Verify: Phone has same user_id
    assert alice_phone['user_id'] == alice['user_id']

    # Verify: Phone can see all groups
    assert group_member.is_member(
        alice['user_id'], group_a_id, alice_phone_peer_id, db
    )
    assert group_member.is_member(
        alice['user_id'], group_b_id, alice_phone_peer_id, db
    )
    assert group_member.is_member(
        alice['user_id'], group_c_id, alice_phone_peer_id, db
    )

    # Verify: Phone has group keys
    assert group_key.has_key(group_a_id, alice_phone_peer_id, db)
    assert group_key.has_key(group_b_id, alice_phone_peer_id, db)
    assert group_key.has_key(group_c_id, alice_phone_peer_id, db)
```

**Test 2: test_link_device_new_groups.py**

Tests that groups created BETWEEN invite creation and acceptance are visible.

```python
def test_link_device_new_groups():
    """
    Scenario:
    1. Alice creates network and group A
    2. Alice creates link invite
    3. Alice creates group B (after invite, before join)
    4. Alice's phone joins via link
    5. Verify phone can see BOTH groups A and B
    """
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Step 1: Alice creates network and group A
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    group_a_id = group.create(
        peer_id=alice['peer_id'],
        group_name='Group A',
        t_ms=2000,
        db=db
    )

    for i in range(5):
        tick.tick(t_ms=2500 + i*100, db=db)

    # Step 2: Create link invite
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        mode='link',
        user_id=alice['user_id'],
        t_ms=3000,
        db=db
    )

    # Step 3: Create group B AFTER invite
    group_b_id = group.create(
        peer_id=alice['peer_id'],
        group_name='Group B',
        t_ms=3500,
        db=db
    )

    for i in range(5):
        tick.tick(t_ms=4000 + i*100, db=db)

    # Step 4: Phone joins
    alice_phone_peer_id, _ = peer.create(t_ms=5000, db=db)
    alice_phone = link.join(
        peer_id=alice_phone_peer_id,
        invite_link=invite_link,
        t_ms=5000,
        db=db
    )

    # Step 5: Sync for convergence
    for i in range(15):  # More rounds for key sharing
        tick.tick(t_ms=6000 + i*200, db=db)

    # Verify: Phone can see both groups
    assert group_member.is_member(
        alice['user_id'], group_a_id, alice_phone_peer_id, db
    )
    assert group_member.is_member(
        alice['user_id'], group_b_id, alice_phone_peer_id, db
    )

    # Verify: Phone has keys for both groups
    assert group_key.has_key(group_a_id, alice_phone_peer_id, db)
    assert group_key.has_key(group_b_id, alice_phone_peer_id, db)
```

**Test 3: test_linked_device_messaging.py**

Tests bidirectional messaging between linked devices.

```python
def test_linked_device_messaging():
    """
    Scenario:
    1. Alice creates network and group
    2. Alice links phone
    3. Alice (primary) sends message M1
    4. Alice (phone) sends message M2
    5. Verify both devices see both messages
    """
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Setup
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    group_id = group.create(
        peer_id=alice['peer_id'],
        group_name='Test Group',
        t_ms=2000,
        db=db
    )

    # Link phone
    invite_id, invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        mode='link',
        user_id=alice['user_id'],
        t_ms=3000,
        db=db
    )

    alice_phone_peer_id, _ = peer.create(t_ms=4000, db=db)
    alice_phone = link.join(
        peer_id=alice_phone_peer_id,
        invite_link=invite_link,
        t_ms=4000,
        db=db
    )

    # Sync for link convergence
    for i in range(10):
        tick.tick(t_ms=5000 + i*100, db=db)

    # Alice (primary) sends message M1
    msg1_id = message.send(
        peer_id=alice['peer_id'],
        group_id=group_id,
        content='Message from primary',
        t_ms=6000,
        db=db
    )

    # Alice (phone) sends message M2
    msg2_id = message.send(
        peer_id=alice_phone_peer_id,
        group_id=group_id,
        content='Message from phone',
        t_ms=6500,
        db=db
    )

    # Sync for message convergence
    for i in range(10):
        tick.tick(t_ms=7000 + i*200, db=db)

    # Verify: Primary device sees both messages
    messages_primary = message.list_messages(
        group_id=group_id,
        peer_id=alice['peer_id'],
        db=db
    )
    assert len(messages_primary) == 2
    assert any(m['content'] == 'Message from primary' for m in messages_primary)
    assert any(m['content'] == 'Message from phone' for m in messages_primary)

    # Verify: Phone sees both messages
    messages_phone = message.list_messages(
        group_id=group_id,
        peer_id=alice_phone_peer_id,
        db=db
    )
    assert len(messages_phone) == 2
    assert any(m['content'] == 'Message from primary' for m in messages_phone)
    assert any(m['content'] == 'Message from phone' for m in messages_phone)
```

**Test 4: test_linked_device_admin_inheritance.py**

Tests that linked devices inherit admin privileges.

```python
def test_linked_device_admin_inheritance():
    """
    Scenario:
    1. Alice creates network (becomes admin via first_peer)
    2. Alice links phone
    3. Bob joins network via invite from Alice
    4. Verify Alice's phone is also admin
    5. Verify Alice's phone can create invites
    """
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Alice links phone
    invite_id, invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        mode='link',
        user_id=alice['user_id'],
        t_ms=2000,
        db=db
    )

    alice_phone_peer_id, _ = peer.create(t_ms=3000, db=db)
    alice_phone = link.join(
        peer_id=alice_phone_peer_id,
        invite_link=invite_link,
        t_ms=3000,
        db=db
    )

    # Sync
    for i in range(10):
        tick.tick(t_ms=4000 + i*100, db=db)

    # Verify: Alice's phone is admin
    assert invite.is_admin(alice['user_id'], alice_phone_peer_id, db)

    # Verify: Alice's phone can create invites
    bob_invite_id, bob_invite_link, _ = invite.create(
        peer_id=alice_phone_peer_id,  # Phone creates invite
        mode='user',
        t_ms=5000,
        db=db
    )

    # Bob joins via phone's invite
    bob_peer_id, _ = peer.create(t_ms=6000, db=db)
    bob = user.join(
        peer_id=bob_peer_id,
        invite_link=bob_invite_link,
        name='Bob',
        t_ms=6000,
        db=db
    )

    # Sync
    for i in range(10):
        tick.tick(t_ms=7000 + i*200, db=db)

    # Verify: Bob successfully joined
    assert bob['user_id'] is not None
    assert group_member.is_member(
        bob['user_id'],
        alice['network_id'],  # all_users group
        bob_peer_id,
        db
    )
```

### Step 8: Run Tests and Debug

1. **Run new scenario tests**:
   ```bash
   pytest tests/scenario_tests/test_link_device_*.py -v
   ```

2. **Common issues to debug**:
   - Group key sharing timing (may need more sync rounds)
   - invite_proof dependency blocking
   - linked_peers table missing entries
   - group_members not inheriting via user_id

3. **Run full test suite**:
   ```bash
   pytest tests/scenario_tests/ -v
   ```

4. **Aim for**: All 4 new tests passing + existing 25 tests still passing = 29+ total passing

## Success Criteria

### Functional Requirements
✅ Linked devices have same user_id, different peer_id
✅ Linked devices see all groups created before link invite
✅ Linked devices see groups created between invite creation and acceptance
✅ Messages sync bidirectionally between linked devices
✅ Admin privileges inherited by linked devices
✅ Linked devices can create invites (if user is admin)

### Test Requirements
✅ 4 new scenario tests passing
✅ Existing 25+ tests still passing
✅ No regressions in convergence behavior

### Code Quality
✅ Unified invite system (no separate link_invite module after migration)
✅ invite_proof correctly handles mode='link'
✅ Clear separation: link.project() handles identity, invite_proof.project() handles proof
✅ Comprehensive test coverage for edge cases

## Migration Strategy

### Phase 1: Implement without breaking existing code
1. Add mode='link' support to invite.create()
2. Implement link.join() using unified invite
3. Keep link_invite.py calling invite.create() internally
4. Run tests to verify no regressions

### Phase 2: Migrate existing code
1. Update any callers of link_invite.create() to use invite.create(mode='link')
2. Update tests to use unified invite API
3. Deprecate link_invite module

### Phase 3: Remove deprecated code
1. Delete link_invite.py after all migrations complete
2. Update documentation to reflect unified invite system

## Open Questions

1. **Group key sharing timing**: Should we share keys immediately when new groups are created, or only when sync happens?
   - **Proposed**: Share immediately in link.join() for groups that exist at join time, rely on normal sync for groups created after

2. **Invite expiration**: Should link invites expire after first use?
   - **Proposed**: No expiration for Phase 1, add expiration in future phase

3. **Multiple devices linking simultaneously**: What happens if two devices link at the same time?
   - **Proposed**: Works correctly (both create separate link events, both get same user_id)

4. **Admin privilege revocation**: If primary device loses admin, should linked device also lose admin?
   - **Proposed**: Yes, admin status is per user_id, not per peer_id

## Timeline Estimate

- Step 1 (Worktree setup): 5 minutes
- Step 2 (Consolidate invite): 2-3 hours
- Step 3 (Implement link.join): 2-3 hours
- Step 4 (Update link.project): 1 hour
- Step 5 (Verify invite_proof): 30 minutes
- Step 6 (Handle new groups): 2-3 hours
- Step 7 (Create tests): 3-4 hours
- Step 8 (Debug and iterate): 4-6 hours

**Total: 15-22 hours** (2-3 working days)

## References

- **Design document**: `/home/hwilson/poc-6/docs/ideal_protocol_design.md`
- **Phase plan**: `/home/hwilson/poc-6/docs/joining_linking_simplification_plan.md`
- **Existing link implementation**: `/home/hwilson/poc-6/events/identity/link.py`
- **Invite implementation**: `/home/hwilson/poc-6/events/identity/invite.py`
- **Invite proof**: `/home/hwilson/poc-6/events/identity/invite_proof.py`
- **Scenario tests**: `/home/hwilson/poc-6/tests/scenario_tests/`
