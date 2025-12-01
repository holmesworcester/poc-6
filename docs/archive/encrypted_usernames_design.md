# Encrypted Usernames Design

**Status: SUPERSEDED - Not Implemented**

**Date Superseded:** 2025-12-01

**Superseded By:** `/docs/planning/encrypted-usernames-identity-architecture-plan.md`

**Reason:** This document described a "hard dependency" model where user events depend on username events. The actual implementation uses a "names as updates" model where:
- User events are source of truth (never blocked)
- Username updates are separate decorating events
- No hard dependency from user to username
- Device names are immutable (not updateable)

This document is retained for historical reference only.

---

# Original Document (Archived)

## Executive Summary

This document proposes a solution for encrypted usernames that meets three core requirements:

1. **All users have a username, always** - No user can exist without a displayable name
2. **Usernames not visible to server nodes** - Usernames encrypted to group members only
3. **Usernames can be updated** - Users can change their username anytime

**Recommended Approach**: Hard Dependency (Option D) - User event BLOCKED until username event exists.

---

## The Problem

### Current State
- `name` field in `users` table stored plaintext in user event
- Visible to anyone who receives the user event (including servers/relay nodes)
- No encryption

### The Bootstrap Problem
To encrypt a username to `all_members` group, the joining user needs:
1. The symmetric group key for `all_members`
2. Knowledge of what `key_id` to encrypt to

But:
- Bob can't encrypt his username until he has the group key
- Bob can't get the group key without syncing
- We want Bob to have a username from the moment he joins

---

## RECOMMENDED: Hard Dependency Approach (Option D)

### Core Concept
User event is **BLOCKED** until username event exists. This guarantees no user can ever appear without a username.

```
┌──────────────────────────────────┐
│    username event (encrypted)    │
│  - name: encrypted to all_members│
│  - signed_by: invite_id          │
└─────────────┬────────────────────┘
              │
              │ user event depends on
              ▼
┌──────────────────────────────────┐
│      user event (unencrypted)    │
│  - depends_on: [username_id]     │
│  - BLOCKED until username exists │
│  - signed_by: invite_id          │
└──────────────────────────────────┘
```

### Why This Works

1. **Impossible to have missing username** - User event literally cannot validate without it
2. **No placeholder UI needed** - Every user always has a displayable name
3. **Atomic join** - Username and user created together from same invite
4. **Clean dependency model** - Follows existing block/unblock validation pattern
5. **All three requirements met** ✅

### Join Flow

```
Bob receives invite link with:
  - invite_private_key (for decryption)
  - invite_prekey_id (crypto hint)
  - Connection info (ip, port, etc.)

Bob's join process:
  1. Connect to Alice
  2. Sync to receive group_key_shared events (fast priority sync)
  3. Decrypt group_key using invite_private_key
  4. Create username event (encrypted to all_members group)
  5. Create user event (depends_on=[username_id])
  6. Both events are synced to peers

Result:
  - Bob has username from moment of join
  - No sync delay or placeholder UI needed
  - User event blocked until username arrives at peers
```

---

## Implementation Details

### Event Formats

#### Username Event (Created FIRST)
```python
username_event = {
    'type': 'username',
    'name': name,                    # Will be encrypted to all_members group
    'created_at': t_ms,
    'signed_by': invite_id,          # Proves invite possession
}
# username_id = hash(username_event)
```

#### User Event (Created SECOND, depends on username)
```python
user_event = {
    'type': 'user',
    'depends_on': [username_id],    # HARD DEPENDENCY - blocks until valid
    'user_pubkey': user_pubkey,
    'peer_id': peer_shared_id,
    'invite_id': invite_id,
    'created_at': t_ms + 1,
    'signed_by': invite_id,
}
# user_id = hash(user_event)
```

#### Username Update Event (Created LATER, any device)
```python
username_update_event = {
    'type': 'username_update',
    'user_id': user_id,              # Now exists!
    'name': new_name,
    'global_count': count,           # For LWW resolution
    'created_at': t_ms,
    'signed_by': peer_shared_id,     # Any linked peer can update
}
```

### Validation Rules

```python
def validate_user_event(event, db):
    """User event MUST have depends_on containing a valid username"""

    # Must have depends_on field
    if 'depends_on' not in event or not event['depends_on']:
        return INVALID

    # Must reference a username event
    username_id = event['depends_on'][0]
    username_event = get_event(username_id, db)

    if not username_event:
        return BLOCKED  # Wait for username to arrive

    if username_event['type'] != 'username':
        return INVALID

    # Both must be signed by same invite (proves atomic creation)
    if username_event['signed_by'] != event['signed_by']:
        return INVALID

    return VALID
```

### Database Schema Changes

#### Users Table (Modified)
```sql
CREATE TABLE users (
    user_id TEXT NOT NULL,
    username_id TEXT NOT NULL,       -- FK to username event
    peer_id TEXT NOT NULL,
    network_id TEXT,
    created_at INTEGER NOT NULL,
    user_pubkey TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, recorded_by)
);
```

#### Usernames Table (New)
```sql
CREATE TABLE usernames (
    username_id TEXT NOT NULL,       -- Event ID (PK)
    user_id TEXT,                    -- Filled in when user event arrives
    name TEXT NOT NULL,              -- Decrypted name
    key_id TEXT NOT NULL,            -- Group key used for encryption
    global_count INTEGER DEFAULT 0,  -- For LWW updates
    created_at INTEGER NOT NULL,
    signed_by TEXT NOT NULL,         -- invite_id or peer_shared_id
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (username_id, recorded_by)
);
```

### Code Changes Required

#### 1. New `username.py` Module
```python
# events/username.py

def create_for_join(name, group_key, t_ms, db):
    """Create initial username event during join"""
    event = {
        'type': 'username',
        'name': name,
        'created_at': t_ms,
        'signed_by': invite_id,
    }
    encrypted = encrypt_to_group(event, group_key)
    username_blob = canonicalize_json(encrypted)
    username_id = store.event(username_blob, peer_id, t_ms, db)
    return username_id

def update(user_id, new_name, peer_id, peer_shared_id, t_ms, db):
    """Update username (any linked device can do this)"""
    # Get current count
    current = db.execute(
        "SELECT global_count FROM usernames WHERE user_id=? ORDER BY global_count DESC LIMIT 1",
        [user_id]
    ).fetchone()

    new_count = (current['global_count'] if current else 0) + 1

    event = {
        'type': 'username_update',
        'user_id': user_id,
        'name': new_name,
        'global_count': new_count,
        'created_at': t_ms,
        'signed_by': peer_shared_id,
    }
    encrypted = encrypt_to_group(event, group_key)
    username_blob = canonicalize_json(encrypted)
    return store.event(username_blob, peer_id, t_ms, db)

def project(event, db):
    """Decrypt and populate usernames table"""
    if event['type'] not in ['username', 'username_update']:
        return

    decrypted = decrypt_to_local(event)  # Uses group keys

    if event['type'] == 'username':
        db.execute("""
            INSERT INTO usernames
            (username_id, name, key_id, global_count, created_at, signed_by, recorded_by, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            event['id'], decrypted['name'], event['key_id'],
            0, event['created_at'], event['signed_by'],
            event['recorded_by'], event['recorded_at']
        ])

    elif event['type'] == 'username_update':
        db.execute("""
            UPDATE usernames
            SET name=?, global_count=?
            WHERE user_id=? AND global_count < ?
        """, [
            decrypted['name'], event['global_count'],
            event['user_id'], event['global_count']
        ])
```

#### 2. Modify `user.py` (Create with Username)
```python
# events/user.py

def create_with_username(name, peer_id, peer_shared_id, invite_data, t_ms, db):
    """Create user + username atomically during join"""

    # Step 1: Decrypt group key from invite
    invite_private_key = crypto.b64decode(invite_data['invite_private_key'])
    group_key = decrypt_group_key_from_invite(
        invite_data['invite_prekey_id'],
        invite_private_key,
        db
    )

    # Step 2: Create username event first
    username_id = username.create_for_join(
        name, group_key, t_ms, db
    )

    # Step 3: Create user event with hard dependency
    user_event = {
        'type': 'user',
        'depends_on': [username_id],  # HARD DEPENDENCY
        'user_pubkey': derive_user_pubkey(invite_private_key),
        'peer_id': peer_shared_id,
        'invite_id': invite_data['invite_id'],
        'created_at': t_ms + 1,
        'signed_by': invite_data['invite_id'],
    }
    signed_user = crypto.sign_event(user_event, invite_private_key)
    user_blob = canonicalize_json(signed_user)
    user_id = store.event(user_blob, peer_id, t_ms + 1, db)

    return user_id, username_id

def validate(event, db):
    """User event validation - check depends_on constraint"""
    if event['type'] != 'user':
        return

    # Check hard dependency
    if 'depends_on' not in event or not event['depends_on']:
        return INVALID

    username_id = event['depends_on'][0]
    username_event = db.execute(
        "SELECT * FROM events WHERE event_id=?", [username_id]
    ).fetchone()

    if not username_event:
        return BLOCKED  # Username hasn't arrived yet

    parsed = json.loads(username_event['content'])
    if parsed.get('type') != 'username':
        return INVALID

    if parsed.get('signed_by') != event['signed_by']:
        return INVALID  # Must be from same invite

    return VALID

def project(event, db):
    """Project user event - includes username lookup"""
    if event['type'] != 'user':
        return

    # Get username_id from depends_on
    depends_on = event.get('depends_on', [])
    username_id = depends_on[0] if depends_on else None

    db.execute("""
        INSERT INTO users
        (user_id, username_id, peer_id, network_id, created_at, user_pubkey, recorded_by, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        event['id'], username_id, event['peer_id'], event.get('network_id'),
        event['created_at'], event['user_pubkey'],
        event['recorded_by'], event['recorded_at']
    ])
```

### Query Patterns

#### Get User with Decrypted Username
```sql
SELECT
    u.user_id,
    u.peer_id,
    u.user_pubkey,
    COALESCE(un.name, 'User ' || substr(u.user_id, 1, 8)) as display_name,
    un.global_count
FROM users u
LEFT JOIN usernames un ON u.username_id = un.username_id
WHERE u.recorded_by = ?
ORDER BY u.created_at
```

#### List All Users in a Network
```sql
SELECT
    u.user_id,
    un.name as username,
    u.created_at
FROM users u
LEFT JOIN usernames un ON u.username_id = un.username_id
WHERE u.network_id = ? AND u.recorded_by = ?
ORDER BY un.name
```

---

## Linked Devices Behavior

### First Device Creates Username
```
Device 1 (Alice):
  1. Receives invite link
  2. Decrypts group key from invite
  3. Creates username event (encrypted, signed by invite_id)
  4. Creates user event (depends_on=[username_id], signed by invite_id)

Result: Alice's user has username from moment of creation
```

### Second Device Inherits Username
```
Device 2 (Alice's iPad):
  1. Creates link_invite from Device 1
  2. Device 2 receives link invite (has same group key material)
  3. Does NOT create new user (user already exists)
  4. Does NOT create new username (username already exists)
  5. Only creates peer_shared event (new device identity)

Result:
  - Device 2 syncs existing user + username
  - Device 2 immediately sees Alice's username
  - Both devices share same user_id and username
```

### Any Device Can Update Username
```
Device 2 wants to change username from "Alice" to "Alice (Work)":
  1. Creates username_update event:
     - user_id: existing user_id
     - name: "Alice (Work)"
     - global_count: prev_count + 1
     - signed_by: device2_peer_shared_id
  2. Encrypts to all_members group
  3. Syncs to all peers

Result:
  - All devices see the update
  - All peers converge to same name (LWW via global_count)
  - Update is atomic and consistent
```

---

## Test Scenarios

### Scenario 1: Basic Join with Username
```
Test: User joins and immediately has displayable username

Setup: Alice has network, Bob receives invite

Steps:
  1. Bob calls user.create_with_username("Bob", ...)
  2. Verify username_id is generated
  3. Verify user event has depends_on=[username_id]
  4. Verify both events are stored
  5. Verify user.project() blocks until username arrives (if simulating peer)

Expected:
  - No BLOCKED state (both created locally)
  - Both events synced to Alice
  - Alice projects both and sees Bob with username
```

### Scenario 2: Username Arrives After User Event
```
Test: Ensure projection waits for username before user is complete

Setup: Simulate receiving user event before username event

Steps:
  1. Receive user event with depends_on=[missing_username_id]
  2. Call user.validate() → should return BLOCKED
  3. Later: receive username event
  4. Call user.validate() again → should return VALID
  5. user.project() succeeds

Expected:
  - User doesn't appear in UI until username arrives
  - No "Unknown User" placeholders
```

### Scenario 3: Linked Device Updates Username
```
Test: Second device can update username from any device

Setup: Alice on Device 1 has username "Alice", Device 2 linked

Steps:
  1. Device 2 calls username.update("Alice (Work)", ...)
  2. Verify username_update event created with global_count=1
  3. Sync to Device 1
  4. Verify both devices show "Alice (Work)"

Expected:
  - Update visible on all devices
  - No separate usernames per device
  - LWW convergence works
```

### Scenario 4: Concurrent Username Updates
```
Test: Multiple devices updating simultaneously converge

Setup: Device 1 and Device 2 both try to update at same time

Steps:
  1. Device 1 updates to "Alice Work" (global_count=2)
  2. Device 2 updates to "Alice Home" (global_count=2)
  3. Both sync to each other
  4. Verify both devices converge to same winner (higher event_id)

Expected:
  - No conflicting usernames
  - All peers see same final value
```

### Scenario 5: Server Node Cannot Read Username
```
Test: Relay/helper node sees encrypted username

Setup: Server node receives username event

Steps:
  1. Server receives username event (encrypted blob)
  2. Server does NOT have group key for all_members
  3. Verify server cannot decrypt name field
  4. Verify server sees encrypted blob in logs/storage

Expected:
  - Server stores encrypted payload
  - Server cannot read plaintext name
  - Privacy maintained
```

---

## Migration & Backwards Compatibility

### For New Users
- Use hard dependency approach exclusively
- Create username + user atomically

### For Existing Users
- Current users have `name` in users table
- Migration: create username event from existing name
- Query logic: prefer usernames table, fallback to users.name
- Old user events continue to work

### Gradual Rollout
```
Phase 1: Deploy new schema + username events + projection
Phase 2: New users use hard dependency approach
Phase 3: Existing users migrated via background job
Phase 4: Remove fallback query logic, require username event
```

---

## Edge Cases & Decisions

### What if server IS in all_members group?
- Server gets the group key
- Server can decrypt usernames
- This is a policy decision, not a technical one
- Recommendation: Don't add servers as normal group members

### What if username_id signature is wrong?
- validate() returns INVALID
- User event stays unblocked
- Never appears in UI

### What if username event is deleted?
- User event becomes orphaned (no valid depends_on)
- User disappears from projections
- Unlikely in practice (immutable event logs)

### What about username length?
- Recommend 1-255 characters
- Enforce at validation layer
- Update only if new name is different

### Can users have the same username?
- Yes, technically allowed
- UI can show truncated user_id to disambiguate
- Recommendation: Client-side warnings, not enforcement

---

## Complexity Analysis

- **Code Changes**: ~400-500 lines
  - New username.py module (~150 lines)
  - Modify user.py (~100 lines)
  - Schema changes (~50 lines)
  - Projections (~100 lines)

- **Database**: 1 new table, 1 modified table

- **Sync**: No changes to sync protocol (normal event propagation)

- **Crypto**: Uses existing group key encryption, no new crypto

- **Testing**: ~8-10 test scenarios

---

## Pros & Cons

### Pros ✅
- **Impossible to have missing username** - user event literally cannot validate without it
- **No placeholder UI needed** - every user always has a displayable name
- **Atomic join** - username and user created together from same invite
- **Clean dependency model** - follows existing block/unblock pattern
- **All three requirements met** ✅

### Cons ⚠️
- Initial username encrypted to invite-time group key (but updates use current key)
- Slightly more complex validation logic
- Two events instead of one for initial join
- Requires understanding of hard dependencies in codebase

---

## Alternative Approaches (Summary)

This section summarizes 11 alternative approaches evaluated. Each is viable for specific use cases, but Hard Dependency is recommended as the baseline.

### Alt 1: Deferred Identity (Async Bootstrap)
**Concept**: User created WITHOUT name initially, username added later in background.

| Aspect | Rating | Note |
|--------|--------|------|
| Simplicity | ⭐⭐⭐⭐⭐ | Fastest to implement |
| User Experience | ⭐⭐ | Placeholder UI required during sync |
| Requirements Met | ⭐⭐ | Fails requirement "always has name" |
| Bootstrap Problem | ✅ Solved | No sync delay |
| Event Count | ⭐⭐⭐⭐⭐ | 1 event (minimal) |

**Use Case**: If placeholders are acceptable and UX jank is tolerable.

---

### Alt 2: Client-Side Nonce Binding
**Concept**: Username encrypted to invite's per-user nonce, then re-encrypted to group key.

| Aspect | Rating | Note |
|--------|--------|------|
| Simplicity | ⭐⭐⭐ | Medium complexity |
| User Experience | ⭐⭐⭐⭐ | Zero sync delay |
| Requirements Met | ⭐⭐⭐⭐⭐ | All three met |
| Bootstrap Problem | ✅ Solved | No bootstrap problem |
| Event Count | ⭐⭐⭐⭐ | 2 events, double encryption |

**Trade-offs**: Double encryption overhead, nonce management complexity.

**Use Case**: If zero-delay join is critical for mobile networks.

---

### Alt 3: Hybrid Public/Private Identity
**Concept**: User has PUBLIC user_id + PRIVATE encrypted name (like Signal sealed senders).

| Aspect | Rating | Note |
|--------|--------|------|
| Simplicity | ⭐⭐⭐ | Separates identity from display |
| User Experience | ⭐⭐⭐⭐ | Good UX |
| Requirements Met | ⭐⭐⭐⭐⭐ | All three met |
| Bootstrap Problem | ✅ Solved | Normal dependency model |
| Event Count | ⭐⭐⭐⭐ | 2 events |

**Trade-offs**: May confuse "user identity" vs "user display name" conceptually.

**Use Case**: If you want to decouple cryptographic identity from social identity strongly.

---

### Alt 4: Signed Username Gossip
**Concept**: Usernames propagated via peer signatures (Byzantine-resistant consensus).

| Aspect | Rating | Note |
|--------|--------|------|
| Simplicity | ⭐ | Very complex consensus logic |
| User Experience | ⭐⭐ | Slow convergence |
| Requirements Met | ⭐⭐⭐⭐ | Met but slowly |
| Bootstrap Problem | ✅ Solved | Gossip handles it |
| Event Count | ⭐ | 3+ events per username |

**Trade-offs**: Requires threshold signatures, slow convergence, overkill for most networks.

**Use Case**: Extremely adversarial networks where peer consensus is critical.

---

### Alt 5: Group-Specific Display Names
**Concept**: Different username per group (like Discord nicknames).

| Aspect | Rating | Note |
|--------|--------|------|
| Simplicity | ⭐⭐ | Very complex per-group tracking |
| User Experience | ⭐⭐⭐⭐⭐ | Familiar Discord-like feature |
| Requirements Met | ⭐⭐⭐⭐⭐ | All three per group |
| Bootstrap Problem | ✅ Solved | Per-group complexity |
| Event Count | ⭐ | N per user (one per group) |

**Trade-offs**: Significantly larger event count, complex queries, UI complexity.

**Use Case**: Multi-group networks wanting per-group identity (future enhancement).

---

### Alt 6: Tiered Encryption (Trust Levels)
**Concept**: Username encrypted to multiple groups at different security levels.

| Aspect | Rating | Note |
|--------|--------|------|
| Simplicity | ⭐⭐⭐ | Three parallel names |
| User Experience | ⭐⭐⭐ | UX burden (choose 3 names) |
| Requirements Met | ⭐⭐⭐⭐ | Partial - flexible privacy |
| Bootstrap Problem | ✅ Solved | Three bootstrap problems |
| Event Count | ⭐⭐ | 3 events |

**Trade-offs**: User must specify 3 versions, metadata leaks which group you're in.

**Use Case**: Networks wanting granular privacy boundaries (public/semi-private/private).

---

### Alt 7: Revocable Username Tokens
**Concept**: Usernames come with validity window + revocation authority.

| Aspect | Rating | Note |
|--------|--------|------|
| Simplicity | ⭐⭐⭐ | Token management adds complexity |
| User Experience | ⭐⭐⭐⭐ | Good if revocation is necessary |
| Requirements Met | ⭐⭐⭐⭐⭐ | All three + revocation |
| Bootstrap Problem | ✅ Solved | Normal dependency |
| Event Count | ⭐⭐⭐⭐ | 2-3 events |

**Trade-offs**: Adds revocation logic, broken UX if username expires without renewal.

**Use Case**: If username history/revocation is important (device loss scenarios).

---

### Alt 8: Ephemeral Display Names + Encrypted Identity
**Concept**: Instant ephemeral names for UX, permanent encrypted names sync in background.

| Aspect | Rating | Note |
|--------|--------|------|
| Simplicity | ⭐⭐⭐⭐ | Clean UX + security |
| User Experience | ⭐⭐⭐⭐⭐ | Zero jank, instant names |
| Requirements Met | ⭐⭐⭐⭐⭐ | All three (appears instantly) |
| Bootstrap Problem | ✅ Solved | Ephemeral + async encrypted |
| Event Count | ⭐⭐⭐⭐ | 2 events |

**Trade-offs**: Requires two parallel naming systems, UI coordination needed.

**Use Case**: Best UX + security hybrid (recommended with Hard Dependency as baseline).

---

### Alt 9: Encrypted Username as "Implicit Group"
**Concept**: Treat username encryption as creating a synthetic "user_identity" group.

| Aspect | Rating | Note |
|--------|--------|------|
| Simplicity | ⭐⭐⭐⭐⭐ | Reuses group machinery |
| User Experience | ⭐⭐⭐⭐ | Good |
| Requirements Met | ⭐⭐⭐⭐⭐ | All three |
| Bootstrap Problem | ✅ Solved | Implicit group handles it |
| Event Count | ⭐⭐⭐⭐ | 1-2 events |

**Trade-offs**: Creates synthetic groups (visual clutter), odd edge cases if user removed.

**Use Case**: Minimal new code approach (if synthetic groups acceptable).

---

### Alt 10: Merkle Tree of Usernames (Version-Safe)
**Concept**: Append-only tree of username versions with cryptographic proofs.

| Aspect | Rating | Note |
|--------|--------|------|
| Simplicity | ⭐ | Very complex tree logic |
| User Experience | ⭐⭐⭐ | Good audit trail |
| Requirements Met | ⭐⭐⭐⭐⭐ | All three + tamper-evident |
| Bootstrap Problem | ✅ Solved | Normal dependency |
| Event Count | ⭐ | N per user (append-only) |

**Trade-offs**: Merkle proof overhead, larger events, slow verification.

**Use Case**: High-auditing requirements (compliance, forensics).

---

### Alt 11: Batch Username Sync
**Concept**: Usernames synced in bulk, validated as batch before projection.

| Aspect | Rating | Note |
|--------|--------|------|
| Simplicity | ⭐⭐⭐ | Adds batch abstraction |
| User Experience | ⭐⭐⭐⭐ | Efficient sync |
| Requirements Met | ⭐⭐⭐⭐⭐ | All three |
| Bootstrap Problem | ✅ Solved | Batch handles bootstrap |
| Event Count | ⭐⭐⭐⭐ | 1-2 events |

**Trade-offs**: Batch abstraction complexity, harder to debug individual failures.

**Use Case**: High-throughput networks with many users (optimization, not baseline).

---

## Comparison Table: All Approaches

| Approach | Always Has Name? | Encrypted? | Event Count | Sync Delay | Complexity | Recommended? |
|----------|------------------|-----------|------------|-----------|-----------|-------------|
| **Hard Dependency** | ✅ Yes | ✅ Yes | 2 | ~1 sync | Medium | ✅ **YES** |
| Async Bootstrap | ❌ Placeholder | ✅ Yes | 1 | None | Low | ❌ |
| Nonce Binding | ✅ Yes | ✅ Yes | 2 | None | High | ❌ |
| Hybrid Public/Private | ✅ Yes | ✅ Yes | 2 | ~1 sync | Medium | ⚠️ Maybe |
| Gossip | ✅ Yes | ✅ Yes | 3+ | High | High | ❌ |
| Group-Specific | ✅ Yes (per-group) | ✅ Yes | N | ~1 sync | High | ⚠️ Future |
| Tiered Encryption | ✅ Yes | ✅ Yes | 3 | ~1 sync | Medium | ❌ |
| Revocable Tokens | ✅ Yes | ✅ Yes | 2-3 | ~1 sync | Medium | ❌ |
| Ephemeral + Encrypted | ✅ Yes (instant) | ✅ Yes | 2 | None (perceived) | Medium | ✅ **Hybrid** |
| Implicit Groups | ✅ Yes | ✅ Yes | 1-2 | ~1 sync | Low | ⚠️ Maybe |
| Merkle Tree | ✅ Yes | ✅ Yes | N | ~1 sync | High | ❌ |
| Batch Sync | ✅ Yes | ✅ Yes | 1-2 | ~1 sync | Medium | ⚠️ Optimization |

---

## Recommendation

### Primary: Hard Dependency (Option D)
- Meets all requirements ✅
- Impossible to have missing username ✅
- No placeholder UI needed ✅
- Clean dependency model ✅
- ~400-500 lines of code

### Secondary (Future Enhancement): Hard Dependency + Ephemeral Names
- Combine hard dependency with instant ephemeral names for perfect UX
- Show "New member" until encrypted username syncs
- Merges best of hard dependency + ephemeral bootstrap

### Not Recommended (for this network)
- Async Bootstrap (breaks requirement)
- Gossip (overkill, slow)
- Tiered Encryption (UX burden)
- Merkle Tree (overkill complexity)

---

## Next Steps

1. **Review this design** - Feedback on hard dependency approach
2. **Clarify alternatives** - If any alternative interests you
3. **Begin implementation** - If approach is approved
   - Create username.py module
   - Modify user.py for hard dependency
   - Add schema migrations
   - Write tests
   - Update CLI/UI for username creation

---

## Questions for Review

1. Does the hard dependency approach feel right for your use case?
2. Would ephemeral names (instant "New member" display) be useful?
3. Should we defer server-node privacy to later, or include now?
4. Do you want per-group display names as a future feature?
5. Any other requirements not covered?
