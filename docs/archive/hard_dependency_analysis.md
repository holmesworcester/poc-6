# Hard Dependency Analysis: Partition Risk & Solutions

**Status: Deep architectural analysis**

## The Partition Problem (Why User Raised It)

### Scenario: Key Rotation + Concurrent Joins

```
Timeline:
T0: Alice creates network
    all_members group key = K1

T1: Bob joins with invite #1
    Bob's invite wrapped K1 → only Bob has K1
    Bob is OFFLINE

T2: Charlie joins with invite #2
    Charlie's invite wrapped K1_new

T3: Alice rotates all_members: K1 → K_new
    Alice deletes K1 (aggressive rotation)
    only has K_new now

T4: Bob comes back online
    Bob's local DB has K1 (from invite #1)
    Bob creates username event encrypted with K1
    Bob syncs to Alice

T5: Alice receives Bob's username event
    It's encrypted with K1
    Alice only has K_new
    Alice CANNOT decrypt Bob's username

T6: Charlie later syncs with Alice
    Charlie never received K1 (joined after rotation)
    Charlie receives Bob's username event (encrypted with K1)
    Charlie CANNOT decrypt it either
```

### The Problem Manifests

**For Hard Dependency Model:**

```python
# Bob's events arrive at Alice
user_event = {
    'type': 'user',
    'depends_on': [username_id],
    'signed_by': invite_id,
}

username_event = {
    'type': 'username',
    'name': <encrypted with K1>,  # K1 = old key, Alice deleted it
    'signed_by': invite_id,
}

# Alice's user.project() runs:
# 1. Check: does username_id exist? YES
# 2. Check: is it type=='username'? YES (can verify from plaintext fields)
# 3. Check: is it signed by correct invite? YES
# 4. ???: Is username decryptable? NO

# Question: Does validation FAIL if username can't be decrypted?
```

**If YES (strict validation):**
- User event stays BLOCKED
- Alice can't validate Bob's username
- Bob's user never appears in Alice's projections
- Permanent partition: Alice has no record of Bob's username
- Charlie joins later, also can't decrypt, accepts partition
- Bob has a user, Alice+Charlie don't recognize Bob's username

**If NO (relaxed validation):**
- User event is VALID even though name can't be read
- User appears in projections
- But Bob's name shows as ??? or placeholder
- No hard partition, but UX broken
- This reverts to "placeholder" problem the hard dependency was trying to solve

### Real Risk Assessment

**Likelihood of actual partition:**
- HIGH if: Aggressive key rotation + concurrent joins + offline devices
- MEDIUM if: Key rotation allows grace period for old keys
- LOW if: Old keys kept in system indefinitely

**Impact if it happens:**
- HIGH: Bob and Alice permanently disagree on Bob's username
- This violates "eventual consistency"
- Requires manual intervention to fix

---

## Network Names: Same Problem?

### Can Hard Dependency Work for Network Names?

**Option 1: Hard Dependency on Network Name**

```python
network_event = {
    'type': 'network',
    'depends_on': [network_name_id],  # Network blocked until name exists
    'created_at': t_ms,
}

network_name_event = {
    'type': 'network_name',
    'name': name,  # encrypted to all_members
    'created_at': t_ms - 1,
    'signed_by': admin_peer,  # or creator
}
```

**Problem:** Network creation is blocked on name, seems awkward
**Partition risk:** Same as usernames (key rotation + concurrent access)

**Option 2: Soft Dependency (Admin-Signed Update)**

```python
network_event = {
    'type': 'network',
    'name_id': null,  # optional, no blocking
    'created_at': t_ms,
}

# Later, admin updates name
network_name_update = {
    'type': 'network_name_update',
    'network_id': network_id,
    'name': new_name,  # encrypted to all_members
    'created_at': t_ms + 1000,
    'signed_by': admin_peer,
}
```

**Advantages:**
- Network can exist without name
- Admin can rename anytime
- No blocking, no partition risk
- Works if network starts unnamed

**Disadvantages:**
- Network initially has no name (placeholder needed)
- Separate event stream for updates

**Recommendation for Network Names:** Use Option 2 (soft dependency with updates)
- Less critical than user identity
- Admin control makes sense
- Avoids partition risk on network creation

---

## The Four Competing Solutions

### SOLUTION 1: Hard Dependency with Aggressive Key Management

**Philosophy**: "Keep hard dependency, but manage keys better"

**Approach**:
1. Hard dependency model stays as-is
2. Change key rotation strategy:
   - Never aggressively delete old group keys
   - Keep old keys in `rotation_history` table
   - Mark keys as 'rotated' but don't delete
   - Any peer with old key can decrypt old messages
   - Explicit 'purge old keys' command (requires consensus)

**Key Management Table**:
```sql
CREATE TABLE group_key_rotation (
    key_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    rotated_at INTEGER,  -- NULL if current, timestamp if rotated
    purged_at INTEGER,   -- NULL if not purged
    key_material BLOB NOT NULL,  -- encrypted backup
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (key_id, recorded_by)
);
```

**Pro/Con Analysis**:

| Aspect | Rating | Notes |
|--------|--------|-------|
| Partition Risk | ✅ Eliminated | Old keys recoverable |
| Code Complexity | ⭐⭐⭐⭐ | Moderate (key management) |
| Storage Overhead | ⭐⭐⭐ | Keys kept around longer |
| User Experience | ✅ Perfect | No placeholders |
| Eventual Consistency | ✅ Guaranteed | All peers eventually see correct name |
| Hard Dependency | ✅ Maintained | Users never appear without names |
| Key Deletion | ⚠️ Manual | Requires explicit purge command |

**When to Use**: Production networks where data loss is unacceptable

**Implementation Complexity**: High
- Track key rotation history
- Allow decryption with old keys
- Add purge mechanism with consensus
- Schema migrations for rotation history

---

### SOLUTION 2: Soft Dependency with Placeholder Usernames

**Philosophy**: "Accept placeholders, eliminate blocking entirely"

**Approach**:
1. Remove hard dependency from user events
2. User events are always VALID, no depends_on field
3. Use deterministic placeholder names until username arrives:
   ```python
   placeholder_name = f"User_{user_id[:8].upper()}"
   # Example: "User_A3F2C1E9"
   ```
4. Username events arrive asynchronously, signed by user's peer
5. Projection replaces placeholder when username received
6. Updates use global_count for LWW

**Event Formats**:

```python
# User event - NO BLOCKING
user_event = {
    'type': 'user',
    'user_pubkey': user_pubkey,
    'peer_id': peer_shared_id,
    'invite_id': invite_id,
    'created_at': t_ms,
    'signed_by': invite_id,
    # NO 'depends_on' field
}

# Username event - arrives when ready
username_event = {
    'type': 'username',
    'user_id': user_id,  # now exists!
    'name': name,  # encrypted to all_members
    'global_count': 0,
    'created_at': t_ms + N,  # can be much later
    'signed_by': peer_shared_id,
}

# Update later
username_update = {
    'type': 'username_update',
    'user_id': user_id,
    'name': new_name,
    'global_count': 1,
    'created_at': t_ms + M,
    'signed_by': peer_shared_id,
}
```

**Validation Logic**:

```python
def validate_user_event(event, db):
    # No hard dependency - always valid if structure is correct
    if event['type'] != 'user':
        return

    if not all(k in event for k in ['user_pubkey', 'peer_id', 'invite_id']):
        return INVALID

    return VALID  # Always succeeds

def project_user(event, db):
    # Create user with placeholder
    placeholder = f"User_{event['id'][:8].upper()}"
    db.execute("""
        INSERT INTO users (user_id, peer_id, user_pubkey, name, ...)
        VALUES (?, ?, ?, ?, ...)
    """, [event['id'], ..., placeholder])

def project_username(event, db):
    # Update user with real name
    name = decrypt_to_local(event['name'])
    db.execute("""
        UPDATE users SET name=? WHERE user_id=?
    """, [name, event['user_id']])
```

**Pro/Con Analysis**:

| Aspect | Rating | Notes |
|--------|--------|-------|
| Partition Risk | ✅ None | No blocking = no partition |
| Code Complexity | ⭐⭐⭐ | Simpler (no hard deps) |
| Storage Overhead | ⭐⭐⭐⭐⭐ | Minimal |
| User Experience | ⭐⭐⭐ | Placeholders visible initially |
| Eventual Consistency | ✅ Guaranteed | All peers eventually same |
| Hard Dependency | ❌ Lost | No guarantee of name at join |
| Key Rotation Resilience | ✅ Perfect | Key timing doesn't matter |

**When to Use**: If placeholder names acceptable, want simplicity

**Implementation Complexity**: Low
- Remove depends_on validation
- Add placeholder generation
- Use LWW for username updates
- ~200 lines of code

---

### SOLUTION 3: Dual Validity (User Valid, Messages Block on Username)

**Philosophy**: "Let user join, but enforce identity on messages"

**Approach**:
1. User events are ALWAYS VALID (no hard dependency)
2. Message events DEPEND ON sender's username
3. Messages BLOCKED until sender has an encrypted username
4. Sender can exist and sync, but their messages aren't visible until their identity is confirmed

**Event Model**:

```python
# User event - always valid
user_event = {
    'type': 'user',
    'user_pubkey': user_pubkey,
    'peer_id': peer_shared_id,
    'invite_id': invite_id,
    'created_at': t_ms,
    'signed_by': invite_id,
}

# Username event - async
username_event = {
    'type': 'username',
    'user_id': user_id,
    'name': name,  # encrypted to all_members
    'global_count': 0,
    'created_at': t_ms + N,
    'signed_by': peer_shared_id,
}

# Message event - blocks on username
message_event = {
    'type': 'message',
    'from_user_id': user_id,
    'body': body,  # encrypted to group
    'depends_on': [username_id],  # REQUIRES sender's username
    'created_at': t_ms,
    'signed_by': peer_shared_id,
}
```

**Validation Logic**:

```python
def validate_message(event, db):
    if event['type'] != 'message':
        return

    # Must have sender
    from_user_id = event['from_user_id']
    user = db.execute("SELECT * FROM users WHERE user_id=?", [from_user_id]).fetchone()
    if not user:
        return BLOCKED  # Sender hasn't been created yet

    # Must have sender's username
    depends_on = event.get('depends_on', [])
    if not depends_on:
        return INVALID  # Message MUST declare username dependency

    username_id = depends_on[0]
    username = db.execute("SELECT * FROM events WHERE event_id=?", [username_id]).fetchone()
    if not username:
        return BLOCKED  # Username hasn't arrived yet

    # Verify username belongs to sender
    if username.user_id != from_user_id:
        return INVALID

    return VALID
```

**Pro/Con Analysis**:

| Aspect | Rating | Notes |
|--------|--------|-------|
| Partition Risk | ✅ None | No blocking on user creation |
| Code Complexity | ⭐⭐⭐⭐ | High (messages block on username) |
| Storage Overhead | ⭐⭐⭐⭐ | Moderate (message dependency tracking) |
| User Experience | ⭐⭐⭐⭐ | Good (user appears but messages hidden initially) |
| Eventual Consistency | ✅ Guaranteed | Messages visible once username arrives |
| Identity Guarantee | ✅ Strong | Every message has identified sender |
| Key Rotation Resilience | ✅ Perfect | No blocking on user creation |

**When to Use**: Want messages always identified, okay with messages arriving after user

**Implementation Complexity**: Medium-High
- Message validation checks username dependency
- Projection engine needs to handle blocked messages
- Queries must skip blocked messages
- ~300 lines of code

---

### SOLUTION 4: Progressive State (User Marked Pending)

**Philosophy**: "User exists but in pending state until username arrives"

**Approach**:
1. User events are ALWAYS VALID
2. User projected to DB with `state: 'pending'`
3. Once username arrives, user `state: 'active'`
4. UI distinguishes pending vs active users
5. Pending users can't send messages, but are visible to network
6. Once active, all pending-time events become visible

**State Model**:

```python
# User record in DB has state
class User {
    user_id: str
    state: 'pending' | 'active'
    username: str  # placeholder until active
    username_id: str  # NULL until active
}

# User event
user_event = {
    'type': 'user',
    'user_pubkey': user_pubkey,
    'peer_id': peer_shared_id,
    'invite_id': invite_id,
    'created_at': t_ms,
    'signed_by': invite_id,
}

# Username event marks user as active
username_event = {
    'type': 'username',
    'user_id': user_id,
    'name': name,  # encrypted to all_members
    'created_at': t_ms + N,
    'signed_by': peer_shared_id,
}
```

**Projection Logic**:

```python
def project_user(event, db):
    placeholder = f"User_{event['id'][:8].upper()}"
    db.execute("""
        INSERT INTO users (user_id, state, username, username_id, ...)
        VALUES (?, 'pending', ?, NULL, ...)
    """, [event['id'], placeholder])

def project_username(event, db):
    name = decrypt_to_local(event['name'])
    db.execute("""
        UPDATE users
        SET state='active', username=?, username_id=?
        WHERE user_id=?
    """, [name, event['id'], event['user_id']])

def query_active_users():
    return db.execute(
        "SELECT * FROM users WHERE state='active'"
    )

def query_all_users(include_pending=False):
    if include_pending:
        return db.execute("SELECT * FROM users")
    else:
        return query_active_users()
```

**UI Logic**:

```javascript
// Show all users but visually distinguish pending
users.forEach(user => {
  if (user.state === 'pending') {
    render_pending_user(user)  // Grayed out, tooltip "Joining..."
  } else {
    render_active_user(user)   // Normal display
  }
})

// Messages from pending users hidden
messages.forEach(msg => {
  const sender = get_user(msg.from_user_id)
  if (sender.state === 'active') {
    render_message(msg)
  }
  // pending messages queued, shown when user becomes active
})
```

**Pro/Con Analysis**:

| Aspect | Rating | Notes |
|--------|--------|-------|
| Partition Risk | ✅ None | No hard blocking |
| Code Complexity | ⭐⭐⭐⭐ | Medium (state tracking) |
| Storage Overhead | ⭐⭐⭐⭐ | Moderate (state field) |
| User Experience | ⭐⭐⭐⭐⭐ | Good (shows pending state) |
| Eventual Consistency | ✅ Guaranteed | Transitions from pending→active |
| Identity Guarantee | ✅ Strong | Active users always identified |
| Key Rotation Resilience | ✅ Perfect | No blocking |

**When to Use**: Want visual feedback on join process, good UX

**Implementation Complexity**: Medium
- Add state field to users table
- Projection logic for state transitions
- UI rendering for pending state
- Query filters for state
- ~250 lines of code

---

## Comparison Matrix: All Five Approaches

| Factor | Hard Dep + Key Mgmt | Placeholders | Messages Block | Progressive State | Hard Dep (Original) |
|--------|-------------------|-------------|-----------------|------------------|------------------|
| **Partition Risk** | ✅ None | ✅ None | ✅ None | ✅ None | ⚠️ POSSIBLE |
| **Requires Name at Join?** | ✅ Yes | ❌ No | ❌ No | ❌ No | ✅ Yes |
| **Code Complexity** | ⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ |
| **Storage Overhead** | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| **UX Quality** | ✅ Perfect | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ✅ Perfect |
| **Eventual Consistency** | ✅ Guaranteed | ✅ Guaranteed | ✅ Guaranteed | ✅ Guaranteed | ⚠️ NOT GUARANTEED |
| **Key Rotation Safe?** | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ❌ NO |
| **Implementation Time** | High | Low | Medium | Medium | Low |
| **Ops Complexity** | ⭐⭐⭐⭐ | ⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐ |

---

## Decision Tree: Which Approach?

```
START: Do you want to guarantee username at join time?
│
├─ YES, but worried about partitions?
│  └─ USE: Hard Dependency + Key Management
│     ✓ Eliminates partition risk
│     ✓ Maintains hard dependency
│     ✗ Requires careful key ops
│
├─ YES, trust you won't partition?
│  └─ USE: Original Hard Dependency
│     ✓ Simplest clean model
│     ✓ Perfect UX
│     ✗ Risk if aggressive rotation
│
├─ NO, placeholders acceptable?
│  ├─ YES, want simplest code?
│  │  └─ USE: Soft Dependency + Placeholders
│  │     ✓ Simplest implementation (~200 lines)
│  │     ✓ Zero partition risk
│  │     ✗ Placeholders visible
│  │
│  └─ NO, want identity guaranteed?
│     └─ Messages Block on Username OR Progressive State
│        CHOOSE BASED ON UX PREFERENCE
│
├─ Want to hide pending users?
│  └─ USE: Progressive State
│     ✓ Cleaner UX (pending vs active)
│     ✓ Messages hidden until active
│
└─ Want messages always visible but unidentified?
   └─ USE: Messages Block on Username
      ✓ Messages visible but blocked content
      ✓ User can join and sync
```

---

## Recommended Path Forward

### Tier 1 (MOST RECOMMENDED): Hard Dependency + Key Management

**Why?**
- Solves the partition risk you identified ✓
- Maintains all benefits of hard dependency ✓
- Eliminates the awkward tradeoffs ✓
- Production-grade solution ✓

**Implementation Plan**:
1. Add `group_key_rotation` table to track key history
2. Modify key rotation logic to "rotate but keep old keys"
3. Explicit purge command with consensus requirement
4. Original hard dependency model unchanged
5. All peers can decrypt old messages

**Risks Mitigated**:
- Concurrent joins with rotation: ✓ Old keys available
- Network partitions: ✓ Eventual consistency guaranteed
- Aggressive key deletion: ✓ Requires consensus

---

### Tier 2 (PRAGMATIC): Progressive State

**Why?**
- If you want to ship faster
- Good UX (pending vs active states)
- Zero partition risk
- Easier ops (no key management complexity)

**Implementation Plan**:
1. User events never block
2. Add `state` field to users table
3. Projection marks users as pending/active
4. UI renders pending state distinctly
5. Messages blocked until active

**Tradeoff**:
- Lose hard dependency guarantee
- Gain simpler operations
- Lose some security property

---

### Tier 3 (IF DESPERATE): Soft Dependency + Placeholders

**Why?**
- Fastest to implement
- Lowest complexity
- Most resilient to any key timing issues

**Implementation Plan**:
1. Remove depends_on from user events
2. Use deterministic placeholders
3. Username events arrive asynchronously
4. Projection replaces placeholder
5. LWW for updates

**Tradeoff**:
- Visible placeholders (jank)
- Lose hard dependency guarantee
- But operation is extremely simple

---

## For Network Names

**Recommendation**: Soft Dependency with Admin Updates

```python
# Network exists independently
network_event = {
    'type': 'network',
    'created_at': t_ms,
    'signed_by': creator_peer,
}

# Admin can update name anytime
network_name_update = {
    'type': 'network_name_update',
    'network_id': network_id,
    'name': name,  # encrypted to all_members
    'created_at': t_ms,
    'signed_by': admin_peer,
}

# Multiple updates → latest wins (by timestamp or sequence)
```

**Why**: Network naming is less critical than user identity, soft dependency is fine.

---

## For Peer Device Names

**Same as Usernames**: Whatever you choose for usernames applies to peer device names.

```python
# Peer event
peer_shared_event = {
    'type': 'peer_shared',
    'user_id': user_id,
    'device_peer_id': peer_id,
    'device_name_id': device_name_id,  # hard or soft?
    'created_at': t_ms,
}

# If hard dependency:
device_name_event = {
    'type': 'device_name',
    'peer_id': peer_id,
    'name': name,  # encrypted to all_members
}
# And peer_shared depends_on=[device_name_id]

# If soft dependency:
# Device name arrives later, placeholder "Device_xyz" until then
```

---

## Final Recommendation Summary

| Aspect | Recommendation |
|--------|-----------------|
| **Username Approach** | Hard Dependency + Key Management (Tier 1) |
| **Network Name** | Soft Dependency + Admin Updates |
| **Peer Device Names** | Same as Usernames (Hard Dep + Key Mgmt) |
| **Fallback if Can't Implement Tier 1** | Progressive State (Tier 2) |
| **If Must Ship Immediately** | Soft Dependency + Placeholders (Tier 3) |

**Key Insight**: The partition problem is REAL and your concern was valid. The solution isn't to abandon hard dependency, but to manage keys better.

---

## Implementation Decision: What Should We Build?

Your choices:

### Option A: Bullet-Proof Hard Dependency
```
+ Hard dependency (impossible to have user without name)
+ No placeholders (perfect UX)
+ Eventual consistency guaranteed
- More complex key management (~150 extra lines)
- Ops cost of key tracking

Effort: 550 lines total
Timeline: 2-3 weeks to get right
```

### Option B: Progressive State (Pragmatic)
```
+ Good UX (pending state visible)
+ Easy to understand
+ Simple operations
- Lose hard dependency guarantee
- Need state management

Effort: 400 lines total
Timeline: 1-2 weeks
```

### Option C: Soft + Placeholders (Fast Ship)
```
+ Simplest code
+ Fastest to implement
+ Extremely resilient
- Placeholders visible (jank)
- Lose hard dependency guarantee

Effort: 280 lines total
Timeline: 1 week
```

**My recommendation**: Go with Option A (Hard Dependency + Key Management) if you have time. It's the most elegant and solves the problem properly. Progressive State is a great fallback if you need to ship faster.

Which would you prefer?
