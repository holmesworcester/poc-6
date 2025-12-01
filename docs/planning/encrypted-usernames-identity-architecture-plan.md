# Encrypted Usernames: Names as Updates Architecture

**Status: Approved Architecture - Implementation Complete (Updated 2025-12-01)**

## Executive Summary

This is the correct and only recommended approach for encrypted usernames, networks, and peer device names.

**Core principle**: Membership (user/network/peer events) is the source of truth and is NEVER blocked by encryption. Names are decorations applied via separate update events that are queued if encrypted.

### Why This Model

Earlier analysis considered hard dependencies (user event blocked until username exists), but that creates:
- **Partition risk**: Key rotation → username unreadable → user stays blocked permanently
- **Circular dependencies**: Membership blocked on decryption
- **Chicken-and-egg**: Can't create user without encrypted name, can't encrypt without user existing

This model eliminates all three problems by inverting the dependency:

**Source of truth for membership must NOT be blocked by encryption.**

This inverts the hard dependency model:

```
WRONG (Hard Dependency):
┌──────────────┐
│ username evt │  ← blocks user creation
│ (encrypted)  │
└──────┬───────┘
       │ depends_on
       ▼
┌──────────────────┐
│   user event     │  ← blocked until name exists
│ (membership)     │
└──────────────────┘

PROBLEM: Key rotation → can't decrypt username → user stays blocked forever


CORRECT (Names as Updates):
┌──────────────────┐
│   user event     │  ← source of truth, NEVER blocked
│ (membership)     │  ← depends on nothing
└────────┬─────────┘
         │ decorated by
         ▼
┌──────────────────────┐
│ username_update evt  │  ← updates/decorates user
│    (encrypted)       │  ← depends on nothing
└──────────────────────┘
         │
         └─→ If key can't decrypt: UI shows placeholder
         └─→ If key rotates: old username unreadable, but user still exists
         └─→ No chicken-and-egg problems


MESSAGES DEPEND ON NAMES (Exception):
┌────────────────┐
│ user event     │
└────────┬───────┘
         │
         ▼
┌────────────────────┐
│username_update evt │
└────────┬───────────┘
         │ message depends on
         ▼
┌────────────────┐
│ message event  │  ← sender must be identified (have username)
└────────────────┘
```

## The Five Core Events (Corrected)

### 1. User Event (Source of Truth)
```python
user_event = {
    'type': 'user',
    'user_pubkey': user_pubkey,
    'peer_id': peer_shared_id,
    'invite_id': invite_id,
    'created_at': t_ms,
    'signed_by': invite_id,
    # NO depends_on field
    # NO name field
}
# user_id = hash(user_event)

# Validation: ALWAYS VALID if structure is correct
def validate_user(event):
    return VALID if has_required_fields(event) else INVALID
```

**Key property**: User event is the SOURCE OF TRUTH for membership. Never blocked, never depends on anything.

---

### 2. Username Update Event (Decoration)
```python
username_update_event = {
    'type': 'username_update',
    'user_id': user_id,  # Identifies which user this name is for
    'name': name,  # encrypted to all_members group
    'created_at': t_ms,
    'signed_by': peer_shared_id,
    'global_count': count,  # for LWW (last-writer-wins on updates)
}

# Validation: ALWAYS VALID if user exists
def validate_username_update(event, db):
    user = get_user(event['user_id'], db)
    if not user:
        return BLOCKED  # Wait for user to exist

    return VALID

# Projection: Update user's display name
def project_username_update(event, db):
    # Try to decrypt with current group keys
    name = decrypt_to_local(event['name'])

    if name is not None:
        # We have the key, store decrypted name
        db.execute("""
            UPDATE users SET display_name=?, username_event_id=?
            WHERE user_id=? AND global_count < ?
        """, [name, event['id'], event['user_id'], event['global_count']])
    else:
        # Key not available yet - store encrypted blob
        # Will be retried when group keys arrive
        db.execute("""
            INSERT INTO pending_username_decrypts
            (user_id, event_id, encrypted_blob, key_id)
            VALUES (?, ?, ?, ?)
        """, [event['user_id'], event['id'], event['name'], event['key_id']])
```

**Key properties**:
- Does NOT block user creation
- Does NOT depend on anything
- User exists immediately, name arrives when key is available
- If key arrives: name decrypted and displayed
- If key not yet available: name queued in pending table
- When group key arrives later (via sync): retry decryption

---

### 3. Message Event (Username Dependency - Future Enhancement)

**Current Implementation Status:** Messages do NOT currently enforce username dependency. This is acceptable for MVP.

```python
# CURRENT: Messages reference user_id but don't require username_update dependency
message_event = {
    'type': 'message',
    'from_user_id': user_id,
    'body': body,  # encrypted to group
    # NOTE: No depends_on field currently enforced
    'created_at': t_ms,
    'signed_by': peer_shared_id,
}

# CURRENT: Validation only checks that user exists
def validate_message(event, db):
    user = get_user(event['from_user_id'], db)
    if not user:
        return BLOCKED
    return VALID
```

**Why This Works for MVP:**
1. Join flow ensures username_update is created immediately after user creation
2. Normal case: Username is decrypted before first message is sent
3. Edge case: If username not yet decrypted, message still valid (user exists)
4. UI can show placeholder ("User_abc123") until username arrives

**Future Enhancement (Not Yet Implemented):**

To ensure all messages have identified senders, validation could be enhanced to require username dependency:

```python
# FUTURE: Message with username dependency enforcement
message_event = {
    'type': 'message',
    'from_user_id': user_id,
    'body': body,  # encrypted to group
    'depends_on': [latest_username_event_id],  # DEPENDS ON sender's username
    'created_at': t_ms,
    'signed_by': peer_shared_id,
}

# FUTURE: Validation blocks until sender has valid username
def validate_message(event, db):
    # Sender must exist
    user = get_user(event['from_user_id'], db)
    if not user:
        return BLOCKED

    # Message must declare username dependency
    depends_on = event.get('depends_on', [])
    if not depends_on:
        return INVALID

    username_id = depends_on[0]
    username = get_event(username_id, db)

    if not username:
        return BLOCKED  # Username hasn't arrived yet

    if username.get('user_id') != event['from_user_id']:
        return INVALID  # Wrong user

    return VALID
```

**Future Enhancement Properties:**
- Messages REQUIRE identified sender
- Sender must have username before message is visible
- Even if sender's name unreadable (key issue), message is blocked
- Ensures all visible messages have identified authors

**Decision:** Username dependency enforcement is deferred to post-MVP. Current behavior is acceptable for initial deployment.

---

### 4. Network Event (Source of Truth)
```python
network_event = {
    'type': 'network',
    'creator_peer_id': creator_peer_id,
    'created_at': t_ms,
    'signed_by': creator_peer_id,
    # NO depends_on
    # NO name
}
# network_id = hash(network_event)

def validate_network(event):
    return VALID if has_required_fields(event) else INVALID
```

---

### 5. Network Name Update Event (Decoration)
```python
network_name_update = {
    'type': 'network_name_update',
    'network_id': network_id,  # Identifies which network this name is for
    'name': name,  # encrypted to all_members
    'created_at': t_ms,
    'signed_by': admin_peer_id,
    'global_count': count,  # for LWW (last-writer-wins on updates)
}

def validate_network_name_update(event, db):
    network = get_network(event['network_id'], db)
    if not network:
        return BLOCKED

    return VALID

def project_network_name_update(event, db):
    try:
        name = decrypt_to_local(event['name'])
        db.execute("""
            UPDATE networks SET display_name=?
            WHERE network_id=? AND global_count < ?
        """, [name, event['network_id'], event['global_count']])
    except DecryptionError:
        # Key unavailable - show placeholder
        pass
```

---

## Join Flow (Corrected)

```
Bob receives invite link with:
  - invite_private_key
  - invite_prekey_id
  - Connection info

Bob's join:
  1. Connect to Alice
  2. Create user event (just identity, no name)
     → Bob now EXISTS in the network ✓

  3. Sync to get group keys

  4. Create username_update event (encrypted with group key)
     → Bob now has a NAME

  5. Create message (depends_on username_update)
     → Bob can send identified messages ✓

Result:
  - Bob's user exists immediately (step 2)
  - Bob's name added asynchronously (step 4)
  - Messages require identity (step 5)
  - No blocking on user creation
  - No chicken-and-egg on key rotation
```

---

## Key Rotation Safe (Why This Works)

### Scenario: Key Rotation During Concurrent Join

```
T0: Alice creates network, all_members has key K1

T1: Bob receives invite #1 (wrapped K1)
    Bob is OFFLINE with K1

T2: Alice rotates: K1 → K_new

T3: Bob comes online
    Bob syncs to Alice
    Alice only has K_new

T4: Bob creates username_update encrypted with K1
    Alice receives it, can't decrypt (no K1 yet)

T5: Alice's projection logic runs:
    name = decrypt_to_local(event['name'])  # Returns None - no K1
    # Store in pending_username_decrypts table
    # Wait for K1 to arrive

T6: Bob syncs and sends Alice the group_key_shared for K1
    Alice receives K1

T7: Alice retries decryption on pending usernames:
    name = decrypt_to_local(event['name'])  # Now succeeds!
    # Update user_names table with "Bob"

T8: Charlie joins later with K_new (doesn't have K1)
    Charlie can't decrypt Bob's username yet
    Charlie sees Bob's username_update in pending table

T9: Resolution:
    a) Bob syncs with Charlie and shares K1
       → Charlie can decrypt and see "Bob"
    b) Bob creates new username_update with K_new
       → Charlie can read new one immediately
    c) Network admin forces K1 key share
       → Charlie gets K1, sees old username

KEY INSIGHT: User Bob still EXISTS and is valid.
Bob's membership is not questioned.
Name will be readable once key arrives.
If key never arrives, name stays pending (but user exists).
Messages can depend on this username_update (blocks until readable).
```

**Comparison to Hard Dependency:**

```
With Hard Dependency (WRONG):
  - Bob's user event BLOCKED
  - Bob doesn't exist in network
  - Permanent partition

With Names as Updates (CORRECT):
  - Bob's user event VALID
  - Bob exists in network
  - Name unreadable (placeholder)
  - Recoverable: Bob can create new username with K_new
```

---

## Linked Devices (With Names as Updates)

### First Device Join
```
Alice Device 1:
  1. Create user event (Alice now exists)
  2. Sync group keys
  3. Create username_update "Alice" (encrypted with group key)
  4. Alice's name available to all
```

### Second Device Joins
```
Alice Device 2:
  1. Create peer_shared event (new device identity)
  2. Sync user event (Alice already exists)
  3. Sync username_update (same Alice)
  4. Device 2 sees Alice's name immediately
```

### Device Updates Username
```
Alice Device 2:
  1. Create new username_update with higher global_count
  2. All devices and peers see update
  3. LWW converges to latest
```

**No changes to linked device logic** - exactly same as before.

---

## Peer Device Names (Immutable Pattern)

**Note:** Device names use a different pattern than usernames. They are immutable.

### Peer Event (Source of Truth - Includes Device Name)
```python
peer_shared_event = {
    'type': 'peer_shared',
    'user_id': user_id,
    'peer_id': peer_id,
    'device_name': device_name,  # Immutable - set at creation time
    'created_at': t_ms,
    'signed_by': invite_id,
}
```

**Key Difference:** Unlike usernames, device names are:
- Set once at peer_shared creation time
- Stored directly in the peer_shared event (not as a separate update event)
- Stored in `peers_shared.device_name` column
- Cannot be updated after creation (immutable)
- Not encrypted (part of public peer identity)

**Rationale:** Device names are identity metadata ("iPhone", "Desktop"), not user-facing display names. They are set during device setup and rarely need to change. This simpler model avoids the complexity of device name update events, LWW resolution, and sync conflicts.

### No peer_name_update Event

**Important:** The `peer_name_update` event type was documented in earlier designs but is **not implemented** and is **not planned** for future implementation. Device names remain immutable as part of peer_shared events.

If mutable device names are needed in the future, the implementation would need to:
1. Add a new `peer_name_update` event type
2. Follow the same pattern as `username_update` (encrypted, LWW via global_count)
3. Store updates in a separate `peer_names` table
4. Handle key rotation and pending decrypts

**Current Status:** Not needed for MVP. Device name immutability is working well in practice.

---

## Network Names (Same Pattern)

### Network Event (Source of Truth)
```python
network_event = {
    'type': 'network',
    'creator_peer_id': peer_id,
    'created_at': t_ms,
}
```

### Network Name Update (Decoration)
```python
network_name_update = {
    'type': 'network_name_update',
    'network_id': network_id,
    'name': name,
    'created_at': t_ms,
    'signed_by': admin_peer_id,  # or any member
}
```

---

## Database Schema (Corrected)

### Users Table (Source of Truth)
```sql
CREATE TABLE users (
    user_id TEXT PRIMARY KEY,
    peer_id TEXT NOT NULL,
    user_pubkey TEXT NOT NULL,
    invite_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
);
```

**No name field!** Name is separate.

### User Names (Decoration)
```sql
CREATE TABLE user_names (
    user_id TEXT NOT NULL,
    name TEXT,                      -- decrypted name
    encrypted_blob BLOB,            -- if encrypted
    event_id TEXT NOT NULL,         -- username_update event ID
    global_count INTEGER NOT NULL,  -- for LWW
    key_id TEXT,                    -- group key used
    created_at INTEGER NOT NULL,
    signed_by TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, recorded_by),
    FOREIGN KEY (user_id) REFERENCES users(user_id),
);
```

### Query Pattern: User with Name
```sql
SELECT
    u.user_id,
    u.peer_id,
    COALESCE(un.name, CONCAT('User_', SUBSTR(u.user_id, 1, 8))) as display_name,
    un.encrypted BOOL,
    un.key_id
FROM users u
LEFT JOIN user_names un ON u.user_id = un.user_id
WHERE u.recorded_by = ?
```

---

## Message Dependency Example

```python
# Alice sends message
# Her username_update is event ID: evt_alice_name_123

message_event = {
    'type': 'message',
    'from_user_id': 'user_alice',
    'body': 'Hello Bob',
    'depends_on': ['evt_alice_name_123'],  # ← Alice's username
    'created_at': t_ms,
    'signed_by': 'peer_device1',
}

# Validation at Bob's peer:
# 1. Does user_alice exist? YES
# 2. Does evt_alice_name_123 exist? WAIT if not
# 3. Is it type='username_update'? YES
# 4. Is it for user_alice? YES
# → Message is VALID and visible

# If Alice updates username:
message_event_2 = {
    'type': 'message',
    'from_user_id': 'user_alice',
    'body': 'I changed my name',
    'depends_on': ['evt_alice_name_124'],  # ← New username event
    'created_at': t_ms,
    'signed_by': 'peer_device1',
}
# New messages point to new username_update
# Old messages still point to old username_update (history preserved)
```

---

## Projection Logic (High Level)

```python
def project_event(event, db):
    """Project any event to DB"""

    if event['type'] == 'user':
        project_user(event, db)
        # User is ALWAYS valid if structure correct
        # NEVER blocked

    elif event['type'] == 'username_update':
        # Try to decrypt with current group keys
        name = decrypt_to_local(event['name'])

        if name is not None:
            # We have the key, store decrypted name
            db.execute(
                "UPDATE user_names SET name=?, key_id=? WHERE user_id=?",
                [name, event['key_id'], event['user_id']]
            )
        else:
            # Key not available yet - store encrypted blob in pending table
            # Will retry when group keys arrive
            db.execute(
                "INSERT INTO pending_username_decrypts "
                "(user_id, event_id, encrypted_blob, key_id) "
                "VALUES (?, ?, ?, ?)",
                [event['user_id'], event['id'], event['name'], event['key_id']]
            )

    elif event['type'] == 'message':
        # Check depends_on
        username_id = event.get('depends_on', [None])[0]
        username = get_event(username_id, db)

        if not username:
            return BLOCKED  # Wait for username

        # Check if username is decrypted
        username_event = db.execute(
            "SELECT * FROM user_names WHERE event_id=?", [username_id]
        ).fetchone()

        if username_event['name'] is None:
            return BLOCKED  # Username not yet decrypted, wait for key

        # Message is valid, store it
        db.execute(
            "INSERT INTO messages (...) VALUES (...)",
            [...]
        )

def validate_event(event, db):
    """Validation gate before projection"""

    if event['type'] == 'user':
        # Always valid
        return VALID if has_required_fields(event) else INVALID

    elif event['type'] == 'username_update':
        # Must have user
        if not get_user(event['user_id'], db):
            return BLOCKED
        return VALID

    elif event['type'] == 'message':
        # Must have sender
        if not get_user(event['from_user_id'], db):
            return BLOCKED

        # Must have sender's username
        username_id = event.get('depends_on', [None])[0]
        if not username_id:
            return INVALID

        if not get_event(username_id, db):
            return BLOCKED

        return VALID
```

---

## Properties of This Architecture

### ✅ No Chicken-and-Egg Problems
- User membership doesn't depend on decryption
- Can create user immediately
- Names added asynchronously
- Key rotation doesn't block user creation

### ✅ Source of Truth is Clear
- User/network/peer events are the source
- Names are updates/decorations
- No circular dependencies
- No blocking on source of truth

### ✅ Key Rotation Safe
- Old encrypted names stored in pending_username_decrypts if key not available
- But users/networks/peers still exist immediately
- Names decrypted once key arrives (via sync)
- Users can update names with new keys if needed

### ✅ Eventual Consistency
- All peers converge to same state
- If some can't decrypt: username in pending table
- When group key arrives: automatic retry decrypts all pending
- User existence never questioned

### ✅ Messages Identified
- Messages depend on sender's latest username
- Ensures sender is always identified
- If sender's name not yet decrypted: message still blocked
- Clean validation logic

### ✅ Linked Devices Work
- All devices share same user_id
- All share same username_update stream
- Any device can update
- LWW converges updates

### ✅ No Placeholders in Normal Flow
- In normal operation: names are readable when username_update arrives
- If key arrives later: automatic decryption (no placeholder needed)
- If key never arrives: username stays in pending (but user exists)
- Very rare edge case (only if key is lost or never shared)

---

## Encryption Details: How Names Are Encrypted

### Where the Key Comes From

When creating a `username_update` event:

```python
def username_update.create(user_id, name, peer_id, peer_shared_id, t_ms, db):
    # Get the all_members group
    all_members = db.execute(
        "SELECT group_id FROM groups WHERE name='all_members'"
    ).fetchone()

    # Get the LATEST key for all_members that THIS peer knows about
    latest_key = db.execute("""
        SELECT key_id, key_material FROM group_keys
        WHERE group_id=? AND rotated_at IS NULL
        ORDER BY created_at DESC LIMIT 1
    """, [all_members['group_id']]).fetchone()

    if not latest_key:
        # Key not available yet - caller must handle
        raise KeyNotAvailableError("all_members group key not yet received")

    # Encrypt name to the group key
    event = {
        'type': 'username_update',
        'user_id': user_id,
        'depends_on': [user_id],  # ← DEPEND ON the user event we're updating
        'name': encrypt_to_group(name, latest_key['key_id']),
        'key_id': latest_key['key_id'],  # Track which key was used
        'global_count': 0,
        'created_at': t_ms,
        'signed_by': peer_shared_id,
    }

    signed_event = crypto.sign_event(event, peer_shared_id)
    return store.event(signed_event, peer_id, t_ms, db)
```

**Key insights**:
1. Each peer uses their **latest known key** for all_members
2. If key rotates, different peers might encrypt with different keys, but eventual consistency still works
3. `username_update` depends on the `user` event it decorates

---

### The Timing Problem: Join Flow Needs Key Before Name Creation

Current issue:
```
1. Bob creates user event (no key needed)
2. Bob syncs, receives group_key_shared
3. Bob NOW HAS KEY
4. Bob can NOW create username_update

Question: How do we trigger step 4 after step 3?
```

**Solution: Use Dependencies, Not Queues**

Instead of tracking pending identities, use the existing dependency system:

1. Try to create `username_update` immediately after sync
2. If key not available: exception bubbles up, caller handles retry
3. If entity doesn't exist: `depends_on` validation blocks it until entity arrives
4. If both key and entity exist: event created normally

### Event Dependencies: The Clean Solution

Instead of pending identity tables and retry logic, use the existing dependency system:

```python
def username_update.create(user_id, name, peer_id, peer_shared_id, t_ms, db):
    # ... get key ...

    event = {
        'type': 'username_update',
        'user_id': user_id,  # ← Identifies which user this name is for
        'name': encrypt_to_group(name, latest_key['key_id']),
        'key_id': latest_key['key_id'],
        'global_count': 0,
        'created_at': t_ms,
        'signed_by': peer_shared_id,
    }
    # ... store and return ...

def network_name_update.create(network_id, name, peer_id, peer_shared_id, t_ms, db):
    # ... get key ...

    event = {
        'type': 'network_name_update',
        'network_id': network_id,  # ← Identifies which network this name is for
        'name': encrypt_to_group(name, latest_key['key_id']),
        'key_id': latest_key['key_id'],
        'global_count': 0,
        'created_at': t_ms,
        'signed_by': peer_shared_id,
    }
    # ... store and return ...

# NOTE: peer_name_update does NOT exist - device names are immutable
# Device names are set in peer_shared.create() and cannot be changed
```

### Validation Logic

Validation checks that the entity (user/network) exists:

```python
def validate_username_update(event, db):
    # Check: Does the user event exist that we're updating?
    user_event = get_event(event['user_id'], db)

    if not user_event:
        return BLOCKED  # Wait for user event to arrive

    if user_event['type'] != 'user':
        return INVALID

    return VALID

def validate_network_name_update(event, db):
    # Check: Does the network event exist that we're updating?
    network_event = get_event(event['network_id'], db)

    if not network_event:
        return BLOCKED  # Wait for network event to arrive

    if network_event['type'] != 'network':
        return INVALID

    return VALID

# NOTE: No validate_peer_name_update - device names are immutable
# Device name validation happens at peer_shared creation time only
```

**How it works**:

1. Try to create `username_update` event:
   - If key missing: `KeyNotAvailableError` raised, caller retries later
   - If key available: event created with `user_id` field set

2. Validate `username_update` event:
   - Check: Does user event (with event_id = user_id) exist?
   - If no: `BLOCKED` (wait for user event to arrive)
   - If yes: `VALID`

3. Project `username_update` event:
   - Only projects if validation was `VALID`
   - Try to decrypt name with current group key
   - If key available: store decrypted name in user_names table
   - If key missing: store encrypted blob in pending_username_decrypts table (retry when key arrives)

### Join Flow with Deterministic Trigger

```
T0: Bob.join() called
    - Create user event (immediately) ✓
    - Try to create username_update → KeyNotAvailableError (no key yet)
    - Store name in pending_name_updates table for later

T1: Bob syncs, receives group_key_shared events
    - group_key_shared is projected by the projector

T2: group_key_shared projection TRIGGERS retry
    - In project_group_key_shared():
      - Normal projection happens
      - Call retry_pending_name_updates(db)
      - This checks pending_name_updates table
      - Key is now available, so try to create pending events

T3: Retry creates username_update
    - Try username_update.create(user_id, name, ...)
    - Key now available ✓
    - Event created with user_id field set

T4: username_update projected normally
    - Decrypts name with group key
    - Stores decrypted name in user_names table
    - Bob's name becomes visible

User experience: Join instant, name appears after sync (usually ~1 second)
Network state: Bob exists (user event) from T0, name visible by T4
```

### Why This Pattern Is Good

1. **Deterministic trigger** - Happens automatically when key arrives (group_key_shared projection)
2. **Pending table** - Stores intent (name for username_update) when key missing
3. **No callback hell** - Trigger is in one place (group_key_shared projector)
4. **Handles all timing** - Entity missing → event blocked, Key missing → stored pending
5. **Descriptive fields** - `user_id`, `network_id`, `peer_id` are clear
6. **Reusable pattern** - Same mechanism for username, network_name, peer_name

---

## Implementation Plan

### Phase 1: Core Events
```python
# events/user.py
- create() → user event, always valid
- Does NOT create username_update

# events/username_update.py (IMPLEMENTED)
- create() → username_update event with user_id field set
- Raises KeyNotAvailableError if group key not available

# events/network_name_update.py (IMPLEMENTED)
- create() → network_name_update event with network_id field set
- Raises KeyNotAvailableError if group key not available

# events/peer_shared.py (IMPLEMENTED - includes device_name)
- create() → peer_shared event with immutable device_name field
- No peer_name_update event - device names cannot be changed
```

### Phase 2: Schema
```sql
- Remove name from users table (IMPLEMENTED)
- Create user_names table (IMPLEMENTED - with optional encrypted_blob for key-missing cases)
- Create network_names table (IMPLEMENTED - similar structure)
- Add device_name to peers_shared table (IMPLEMENTED - immutable field)
- Create pending_name_decrypts table (IMPLEMENTED - for queued decryptions when key arrives later)
```

**Note:** No separate peer_names table needed - device names stored directly in peers_shared.

### Phase 3: Validation
```python
# Validate username_update: (IMPLEMENTED)
# - Check: Does the user event (with event_id = user_id) exist?
# - If missing: BLOCKED
# - If exists and type is 'user': VALID
# - If exists but wrong type: INVALID

# Same pattern for network_name_update (IMPLEMENTED)

# NOTE: No peer_name_update validation - device names are immutable
# Device name validation happens at peer_shared creation time only
```

### Phase 4: Projection
```python
# User projection: always succeeds (IMPLEMENTED)

# Username_update projection: (IMPLEMENTED)
# - IF validation returned VALID (user event exists):
#   - Try to decrypt name with current group key
#   - If successful: store in user_names table
#   - If key missing: store encrypted blob in pending_name_decrypts table
# - IF validation returned BLOCKED (user event doesn't exist):
#   - Skip projection (projector will retry when user event arrives)

# Network_name_update projection: (IMPLEMENTED - same pattern as username_update)

# Peer_shared projection: (IMPLEMENTED - device_name stored directly)
# - Device name stored in peers_shared.device_name column
# - No separate projection needed for device names

# Message projection: (CURRENT BEHAVIOR - no username dependency)
# - Message validation checks user exists
# - No depends_on field currently required
# - Future enhancement: Add username dependency validation
```

### Phase 5: Deterministic Trigger on Key Arrival

**The trigger**: When `group_key_shared` event is projected, automatically retry pending identity updates.

**In group_key_shared projector:**
```python
def project_group_key_shared(event, db):
    # Normal projection of group key material
    # ... existing logic ...

    # NEW: Deterministic trigger - retry any pending identity updates
    # This happens AFTER the key is available
    retry_pending_name_updates(db)

def retry_pending_name_updates(db):
    """Try to create pending identity updates now that key is available."""
    pending = db.execute("""
        SELECT * FROM pending_name_updates
        WHERE status='waiting_for_key'
    """).fetchall()

    for item in pending:
        try:
            if item['type'] == 'username':
                username_update.create(
                    item['entity_id'], item['name'],
                    item['peer_id'], item['peer_shared_id'],
                    time_ms(), db
                )
            elif item['type'] == 'network_name':
                network_name_update.create(
                    item['entity_id'], item['name'],
                    item['peer_id'], item['peer_shared_id'],
                    time_ms(), db
                )
            # NOTE: No peer_name case - device names are immutable

            # Mark as created
            db.execute("""
                UPDATE pending_name_updates SET status='created'
                WHERE id=?
            """, [item['id']])

        except KeyNotAvailableError:
            # Still don't have key (shouldn't happen, but be safe)
            pass

        except Exception as e:
            # Other error - mark as failed
            db.execute("""
                UPDATE pending_name_updates SET status='failed', error=?
                WHERE id=?
            """, [str(e), item['id']])
```

**During Join:**
```python
def join(peer_id, peer_shared_id, invite_data, name, t_ms, db):
    # Step 1: Create user event (always works)
    user_id = user.create(peer_id, peer_shared_id, invite_data, t_ms, db)

    # Step 2: Try to create username_update (might fail if key not available)
    try:
        username_update.create(user_id, name, peer_id, peer_shared_id, t_ms, db)
        # Success! Event created, will be validated and projected
    except KeyNotAvailableError:
        # Key not available yet - store for later creation
        db.execute("""
            INSERT INTO pending_name_updates
            (type, entity_id, name, peer_id, peer_shared_id, status, ...)
            VALUES ('username', ?, ?, ?, ?, 'waiting_for_key', ...)
        """, [user_id, name, peer_id, peer_shared_id])
        # Deterministic trigger will handle it when group_key_shared arrives

    # Step 3: Sync (receives group_key_shared events)
    sync(...)
    # ← group_key_shared projection automatically calls retry_pending_name_updates()
```

**For Network Names (Admin-initiated):**
```python
def update_network_name(network_id, new_name, peer_id, peer_shared_id, t_ms, db):
    try:
        network_name_update.create(network_id, new_name, peer_id, peer_shared_id, t_ms, db)
        # Success! Event created, will be validated and projected
    except KeyNotAvailableError:
        # Key not available yet - store for later creation
        db.execute("""
            INSERT INTO pending_name_updates
            (type, entity_id, name, peer_id, peer_shared_id, status, ...)
            VALUES ('network_name', ?, ?, ?, ?, 'waiting_for_key', ...)
        """, [network_id, new_name, peer_id, peer_shared_id])
        # Deterministic trigger will handle it when group_key_shared arrives
```

**Note on Device Names:**
Device names are NOT updateable. They are set once at peer_shared creation:

```python
# Device names are immutable - set at peer_shared creation only
peer_shared_id = peer_shared.create(
    peer_id=peer_id,
    t_ms=t_ms,
    db=db,
    invite_id=invite_id,
    invite_private_key=invite_private_key,
    device_name="iPhone"  # Set once, cannot be changed
)
# No update_peer_name function exists - device names are immutable
```

### Phase 6: Query Layer
```python
# Get user with current name (handles missing decrypt):
SELECT
    u.user_id,
    u.peer_id,
    COALESCE(un.name, 'User_' || substr(u.user_id, 1, 8)) as display_name
FROM users u
LEFT JOIN user_names un ON u.user_id = un.user_id
WHERE u.recorded_by = ?
```

---

## Example Flow: Multi-Device Rotation

```
T0: Alice Device 1, key = K1
    - Create user event → Alice exists
    - Create username_update "Alice" encrypted with K1
    - All devices sync, see "Alice" decrypted

T1: Alice Device 2 links
    - Sync user event → Alice exists ✓
    - Sync username_update + K1 → see "Alice" decrypted ✓

T2: Bob joins with invite (has K1 from invite)
    - Create user event → Bob exists ✓
    - Sync: receives K1 (from Alice or in invite)
    - Create username_update "Bob" encrypted with K1
    - All peers decrypt and see "Bob" ✓

T3: Alice rotates K1 → K_new
    - Update all_members key material
    - Existing messages stay readable (with K1)
    - Bob's and Alice's old usernames still readable (have K1)

T4: Charlie joins with new invite (has K_new, NOT K1)
    - Create user event → Charlie exists ✓
    - Sync group keys (has K_new, not K1)
    - Bob's username_update event arrives (encrypted with K1)
    - Charlie can't decrypt → stored in pending_username_decrypts table
    - Charlie sees Bob exists but username unreadable (has event but no key)

T5: Bob comes back online
    - Syncs all events and group keys
    - If Bob has K1: already decrypted
    - Optionally: Bob creates new username_update "Bob" with K_new
    - Charlie can now decrypt new version ✓

T6: Alice Device 2 updates name
    - Creates username_update "Alice (Work)" with K_new
    - Device 1, Bob, and Charlie all have K_new
    - All decrypt and see "Alice (Work)" ✓

Result: No permanent partitions, everyone converges, no chicken-and-egg.
Bob's user exists immediately. Bob's name readable when key available.
```

---

## Decision: Is This the Right Model?

### This model is SUPERIOR because:

1. **Membership is never blocked** ✅
   - Users exist independently of names
   - Networks exist independently of names
   - Peers exist independently of names

2. **Key rotation is safe** ✅
   - Users/networks/peers stay readable
   - Names queued in pending table if key unavailable
   - Automatic retry when group key arrives
   - No placeholders in normal flow
   - Can update with new keys if needed

3. **Source of truth is clear** ✅
   - Events → user/network/peer (never blocked)
   - Updates → names/properties (decorated by)
   - No circular dependencies

4. **Eventual consistency** ✅
   - All peers converge on membership
   - Names resolve when keys arrive
   - No permanent disagreement

5. **Messages can be identified** ✅
   - Optional: messages depend on latest username
   - Messages blocked until sender name decrypted
   - Ensures identified senders

6. **Clean decryption pipeline** ✅
   - Username events queued if key missing
   - Automatic retry when group key arrives
   - No placeholders needed (keys eventually arrive)

---

## Recommendation

**This is the only recommended approach.**

### Why This Is Right

- ✅ **Architecturally sound** - Clear source of truth, no circular dependencies
- ✅ **Key-rotation safe** - User exists even if name unreadable temporarily
- ✅ **No partition risk** - Membership never blocked, only names queued
- ✅ **Eventual consistency** - All peers converge once keys available
- ✅ **Simple to implement** - ~300 lines, straightforward logic
- ✅ **Handles all cases** - Linked devices, device names, network names

### Implementation Requirements

- `pending_username_decrypts` table for queued encrypted names
- Retry/retry-all logic triggered when group keys arrive
- LWW (last-writer-wins) via global_count for username updates

### Core Principle

```
User/Network/Peer Event (source of truth)
    ↓ (decorated by)
Username_update Event (queued if encrypted, retried when key arrives)
    ↓ (optionally depended on by)
Message Event (blocks until sender identified)
```

Membership is NEVER blocked. Names are always queued if encrypted, not discarded.

---

## Appendix: Why Other Approaches Don't Work

### Hard Dependency (User Blocked Until Username Exists)
- **Problem**: Membership depends on decryption of encrypted event
- **Risk**: Key rotation → username unreadable → user permanently blocked
- **Result**: Network partition (Alice sees Bob, Charlie doesn't)
- **Status**: ❌ Rejected

### Soft Dependency + Placeholders
- **Problem**: Users show as "User_xyz" until name arrives
- **Jank**: UI clutter, confusing user experience
- **Better alternative**: Queue encrypted names, no placeholder needed
- **Status**: ❌ Rejected (this model superior)

### Async Bootstrap (User Created Without Name)
- **Problem**: Same as soft dependency but worse
- **Result**: Extended jank during sync
- **Status**: ❌ Rejected

### Client-Side Nonce Binding
- **Problem**: Double encryption overhead, complex nonce management
- **Benefit**: Zero sync delay (not worth the complexity)
- **Status**: ❌ Rejected (over-engineered)

### Gossip Consensus
- **Problem**: Requires Byzantine agreement, very complex
- **Result**: Slow convergence, overkill for most networks
- **Status**: ❌ Rejected (unnecessary complexity)

### Group-Specific Display Names (Per-Group Nicknames)
- **Note**: This is a valid FUTURE enhancement, not part of baseline
- **Can be added later**: Once usernames working, per-group variants are straightforward
- **Status**: ⚠️ Deferred (out of scope for initial implementation)

### Encrypted Username as "Implicit Group"
- **Problem**: Synthetic groups add visual/operational clutter
- **Benefit**: Minimal new code (reuses group machinery)
- **Verdict**: Simpler to use explicit username_update events
- **Status**: ❌ Rejected

---

## Notes for Implementation

For detailed analysis of alternatives that were considered, see `archive/` directory (historical analysis only, not part of approved design).

This architecture has been validated against:
- Key rotation scenarios
- Multi-device linking
- Network partitions
- Concurrent joins
- Linked device updates

**Implementation Status: COMPLETE**

This architecture has been fully implemented with the following notes:

### Implemented Features:
1. ✅ Usernames as updates (username_update events)
2. ✅ Network names as updates (network_name_update events)
3. ✅ Device names as immutable fields (stored in peer_shared events)
4. ✅ Pending name decrypts table for key-missing scenarios
5. ✅ Retry logic when group keys arrive
6. ✅ LWW (last-writer-wins) for username and network name updates

### Deviations from Original Design:
1. **Device names are immutable** - Not updateable via peer_name_update events (as documented in earlier versions). This is a simpler and more appropriate design for device identity metadata.
2. **Messages don't enforce username dependency** - Messages currently reference user_id but don't require depends_on=[username_update_id]. This is acceptable for MVP and can be added as a future enhancement if needed.

### Current Behavior:
- Users join with username created immediately (via username_update)
- Device names set once at peer_shared creation time
- Network names updateable by admins
- All names encrypted to all_members group
- Key rotation safe (names queue in pending table if key missing)

**No further architectural changes are expected.** Implementation is complete and working as designed.
