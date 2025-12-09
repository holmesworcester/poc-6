# Logical Key Hierarchy (LKH) for O(log n) User Removal

## Problem Statement

The current removal implementation is O(n): when a user is removed, `rotate_for_removal()` creates a new group key and then `share_key_with_group_members()` creates a separate `group_key_shared` event sealed to each remaining member's prekey.

For a group with 1000 members, removing one user generates ~999 key sharing events.

LKH provides **O(log n) removal** by using a tree structure where keys are distributed via sibling encryption rather than individual prekeys.

## Solution: Logical Key Hierarchy (LKH)

LKH is a well-studied broadcast encryption scheme native to tree structures.

### Core Insight

Instead of deriving keys top-down (which lets anyone with an ancestor key derive descendants), use **independent random keys** at each node, with re-keying messages encrypted to sibling subtrees.

```
        K_root                    ← random, not derived
        /    \
      K_0    K_1                  ← random, independent
      / \    / \
   K_00 K_01 K_10 K_11           ← random, user leaf keys
    |    |    |    |
   U_0  U_1  U_2  U_3
```

Each user stores O(log n) keys: their path from leaf to root.

### Removal with Forward Secrecy

Remove U_1:

```
1. Generate NEW random keys for U_1's path: K_01', K_0', K_root'

2. Distribute via sibling encryption:
   - K_0'    encrypted with K_00  → only U_0 can decrypt
   - K_root' encrypted with K_1   → only U_2, U_3 can decrypt

3. New group keys use K_root'
```

**Why U_1 can't decrypt:**
- U_1 has: K_01, K_0, K_root (old keys)
- U_1 does NOT have: K_00, K_1 (sibling keys)
- U_1 cannot decrypt any re-key message
- U_1 cannot obtain K_root'

**O(log n) events** for removal. Forward secrecy achieved.

## Event Types

### tree_key (Local-only)

Stores actual key material. Not synced.

```python
tree_key = {
    'type': 'tree_key',
    'node_path': str,        # e.g., "LLR" for left-left-right from root
    'version': int,          # increments on re-key
    'key': bytes,            # 32-byte symmetric key
    'group_id': str,         # which group's tree
    # id = hash(node_path || version || group_id || key)
}
```

### tree_key_shared (Shared)

Re-keying announcement, encrypted to sibling subtree.

```python
tree_key_shared = {
    'type': 'tree_key_shared',
    'node_path': str,
    'version': int,          # new version being announced
    'group_id': str,
    'encrypted_key': bytes,  # new key encrypted to SIBLING's current key
    'sibling_path': str,     # which sibling was used
    'sibling_version': int,  # which version of sibling
    'signed_by': str,        # admin peer_shared_id
    'created_at': int,
}
```

### group_key_tree_shared (Shared)

Group key wrapped to tree root.

```python
group_key_tree_shared = {
    'type': 'group_key_tree_shared',
    'key_id': str,           # references group_key
    'group_id': str,
    'encrypted_key': bytes,  # group key encrypted to root tree key
    'root_version': int,     # which version of root
    'signed_by': str,
    'created_at': int,
}
```

## Operations

### Join Flow - Detailed Analysis

**Key question:** Can users add themselves to the tree when they join?

**Answer:** No. The joiner doesn't know the tree state (which slots are empty, current tree depth). The **inviter** must assign the leaf position.

#### Current invite flow (for reference)

1. Inviter creates `invite` event with `invite_prekey_id` (a public key)
2. Inviter creates `group_key_shared` events sealed to `invite_prekey_id`
3. Invite link contains `invite_private_key`
4. Joiner uses private key to decrypt `group_key_shared` events
5. Joiner now has group keys and can decrypt messages

#### LKH invite flow

Same pattern, but instead of sharing `group_key` directly, share `tree_key` events:

```python
def create_invite_with_lkh(peer_id, group_id, t_ms, db):
    # 1. Assign leaf position for future joiner
    leaf_path = assign_leaf_for_invite(group_id, db)

    # 2. Create invite with leaf assignment embedded
    invite_prekey_id, invite_private_key = group_prekey.create(peer_id, t_ms, db)

    invite_event_data = {
        'type': 'invite',
        'invite_prekey_id': invite_prekey_id,
        'leaf_path': leaf_path,  # NEW: pre-assigned tree position
        'group_id': group_id,
        # ... other fields
    }
    invite_id = store.event(invite_event_data, ...)

    # 3. Share O(log n) tree keys sealed to invite prekey
    for node_path in path_from_leaf_to_root(leaf_path):
        tree_key = get_current_tree_key(group_id, node_path, db)
        create_tree_key_shared_for_invite(
            node_path=node_path,
            version=tree_key.version,
            key=tree_key.key,
            invite_id=invite_id,  # extracts invite_prekey for sealing
            db=db
        )

    return invite_id, invite_link_data
```

#### Problem: Invite links are multi-use

**Critical constraint:** Invite links can be used by multiple people. The same link might be:
- Shared in a group chat
- Posted on a website
- Printed as a QR code at an event

This breaks the "inviter assigns leaf" model - we can't pre-assign a leaf position when we don't know how many people will use the invite.

#### Solution: Defer leaf assignment to join time

The inviter **cannot** assign a leaf position at invite creation. Instead:

1. **Invite creation**: Share tree keys for **all current leaves' paths** (or use a different approach - see below)
2. **Join time**: The **joiner** (or first existing member they sync with) assigns a leaf

But wait - if we share keys for all paths, that's O(n) keys, defeating the purpose of LKH.

#### Better solution: Two-phase join

**Phase 1: Bootstrap sync (using existing O(n) mechanism)**
1. Joiner uses invite to get `group_key` (current O(n) approach works)
2. Joiner syncs, becomes a member, can decrypt messages
3. Joiner does NOT yet have tree position

**Phase 2: Tree enrollment (async, by any admin)**
1. Any admin sees new member without tree position
2. Admin assigns leaf, creates `tree_leaf_assignment` event
3. Admin shares O(log n) tree keys to joiner's `group_prekey`
4. Joiner receives keys, now enrolled in tree

**Why this works:**
- Multi-use invites work (no pre-assigned leaf)
- Join is instant (uses existing group_key mechanism)
- Tree enrollment is O(log n) per user
- Removal is O(log n)

**Trade-off:** New members aren't in tree until enrolled. If removed before enrollment, removal is still O(n) for them. But this is rare - enrollment can happen within seconds of join.

#### Recommended: Lazy TreeKEM with O(n) fallback

Use the tree as first resort, with `group_key_shared` as fallback for users not yet enrolled.

**Key distribution:**
```
group_key
    ├── group_key_tree_shared (wrapped to tree root) ─── O(1), for enrolled users
    └── group_key_shared (per non-enrolled user) ─────── O(1) each, fallback
```

**When are users enrolled?**

Keys only change via admin actions:
1. User removal
2. Key rotation (expiry, PCS updates)

The admin performing a key change can **batch-enroll** any pending users:
1. Assign leaf positions to all non-enrolled members
2. Share O(log n) tree keys to each via their `group_prekey`
3. Re-key tree (if removal)
4. Wrap new `group_key` to tree root
5. Fallback `group_key_shared` only for anyone still not enrolled (rare edge case)

**Complexity:**
| Operation | Events |
|-----------|--------|
| Join | O(1) - `group_key_shared` to invite prekey |
| Send message | O(1) - wrapped to tree root |
| Removal (all enrolled) | O(log n) - tree re-key |
| Removal (some not enrolled) | O(log n) + O(k) where k = non-enrolled users |
| Batch enrollment | O(k log n) - k users × log n tree keys each |

**Why this works:**
- Async self-join preserved (invite link works without online admin)
- Multi-use invites work (no pre-assigned leaf)
- Tree enrollment happens naturally during admin key operations
- Falls back gracefully when tree isn't fully populated

**Edge cases:**
- User joins → immediately removed before any admin key operation → O(n) removal for them (rare)
- Many users join between key changes → batch enrollment amortizes cost

#### What the invite shares

With deferred enrollment, the invite just shares the current `group_key` (as today). Tree keys are shared separately when an admin enrolls the user.

```python
def create_invite(peer_id, group_id, t_ms, db):
    # Same as today - share group_key sealed to invite_prekey
    # No tree keys yet (leaf not assigned)
    ...
```

#### Joiner's perspective (unchanged from today)

1. Click invite link (contains `invite_private_key`)
2. Create local `group_prekey` from invite private key (deterministic)
3. Sync with any peer (or server)
4. Receive and decrypt `group_key_shared` events using invite private key
5. Now have `group_key`, can decrypt messages immediately
6. Later: receive `tree_key_shared` events when admin enrolls them

The joiner can read/write messages immediately. Tree enrollment happens in background.

#### Admin enrollment flow

When an admin performs any key-changing operation (removal, rotation), they batch-enroll pending users:

```python
def rotate_group_key_with_enrollment(group_id, admin_peer_id, t_ms, db,
                                      removed_user_id=None):
    """Called by admin for key rotation or removal. Enrolls pending users."""

    # 1. Find users not yet in tree
    pending_users = get_users_without_tree_position(group_id, db)

    # 2. Batch enroll them
    for user_id in pending_users:
        enroll_user_in_tree(user_id, group_id, admin_peer_id, t_ms, db)

    # 3. If removal, re-key the removed user's path
    if removed_user_id:
        removed_leaf = get_user_leaf_path(removed_user_id, group_id, db)
        rekey_path_for_removal(removed_leaf, group_id, admin_peer_id, t_ms, db)
        mark_leaf_empty(removed_leaf, group_id, db)

    # 4. Create new group key
    new_key_id = group_key.create(admin_peer_id, t_ms, db)

    # 5. Wrap to tree root (O(1) - all enrolled users can decrypt)
    root_key = get_tree_key(group_id, "", db)  # "" = root path
    create_group_key_tree_shared(
        key_id=new_key_id,
        group_id=group_id,
        encrypted_key=encrypt(new_key_id, root_key),
        root_version=root_key.version,
        db=db
    )

    # 6. Fallback for any still-not-enrolled (shouldn't happen normally)
    still_pending = get_users_without_tree_position(group_id, db)
    for user_id in still_pending:
        user_prekey = get_group_prekey_for_user(user_id, admin_peer_id, db)
        group_key_shared.create(new_key_id, user_prekey, db)


def enroll_user_in_tree(user_id, group_id, admin_peer_id, t_ms, db):
    """Assign leaf position and share tree keys to user."""

    # Assign leaf (reuse empty slot or next sequential)
    leaf_path = assign_leaf(group_id, db)

    # Create tree_leaf_assignment event (shareable, so all peers see it)
    create_tree_leaf_assignment(
        user_id=user_id,
        leaf_path=leaf_path,
        group_id=group_id,
        signed_by=admin_peer_id,
        t_ms=t_ms,
        db=db
    )

    # Get user's group_prekey for sealing tree keys
    user_prekey = get_group_prekey_for_user(user_id, admin_peer_id, db)

    # Share O(log n) tree keys for their path
    for node_path in path_from_leaf_to_root(leaf_path):
        tree_key = get_current_tree_key(group_id, node_path, db)
        create_tree_key_shared_to_prekey(
            node_path=node_path,
            version=tree_key.version,
            key=tree_key.key,
            recipient_prekey=user_prekey,
            group_id=group_id,
            db=db
        )
```

#### Removal flow with tree

```python
def remove_user(removed_user_id, group_id, admin_peer_id, t_ms, db):
    """Remove user with O(log n) tree re-keying."""

    # Check if user is in tree
    leaf_path = get_user_leaf_path(removed_user_id, group_id, db)

    if leaf_path is None:
        # User never enrolled - just exclude from group_key_shared
        # (rare: they joined and got removed before any key rotation)
        rotate_group_key_excluding_user(removed_user_id, group_id,
                                         admin_peer_id, t_ms, db)
        return

    # User is in tree - re-key their path
    rekey_path_for_removal(leaf_path, group_id, admin_peer_id, t_ms, db)

    # Mark their leaf as empty (available for reuse)
    mark_leaf_empty(leaf_path, group_id, db)

    # Create user_removed event
    create_user_removed(removed_user_id, admin_peer_id, t_ms, db)

    # Rotate group key (will be wrapped to new tree root)
    rotate_group_key_with_enrollment(group_id, admin_peer_id, t_ms, db)


def rekey_path_for_removal(leaf_path, group_id, admin_peer_id, t_ms, db):
    """Re-key every node on removed user's path, encrypted to siblings."""

    # Walk from leaf to root
    for node_path in path_from_leaf_to_root(leaf_path):
        sibling_path = get_sibling_path(node_path)

        if sibling_path is None:
            continue  # root has no sibling

        # Check if sibling subtree has any users
        if is_subtree_empty(sibling_path, group_id, db):
            continue  # no one to encrypt to

        sibling_key = get_tree_key(group_id, sibling_path, db)

        # Generate new key for this node
        new_key = crypto.random_bytes(32)
        new_version = get_current_version(group_id, node_path, db) + 1

        # Create tree_key_shared encrypted to sibling
        # (everyone in sibling subtree can decrypt via their path)
        create_tree_key_shared(
            node_path=node_path,
            version=new_version,
            group_id=group_id,
            encrypted_key=encrypt(new_key, sibling_key.key),
            sibling_path=sibling_path,
            sibling_version=sibling_key.version,
            signed_by=admin_peer_id,
            db=db
        )

        # Store locally
        store_tree_key(node_path, new_version, group_id, new_key, db)
```

#### Projection of tree_key_shared

```python
def project_tree_key_shared(event_id, recorded_by, recorded_at, db):
    """Project tree_key_shared - decrypt if we're in the sibling subtree."""

    event = get_event_data(event_id, db)

    # Check if we have the sibling key (meaning we're in that subtree)
    sibling_key = get_tree_key(
        group_id=event['group_id'],
        node_path=event['sibling_path'],
        version=event['sibling_version'],
        db=db
    )

    if sibling_key is None:
        # We don't have the sibling key - either:
        # 1. We're the removed user (can't decrypt)
        # 2. We're in a different subtree (will get different re-key event)
        # 3. We haven't synced the sibling key yet (will retry later)
        return VALID  # Event is valid, we just can't use it yet

    # Decrypt the new key
    new_key = decrypt(event['encrypted_key'], sibling_key.key)

    # Store it
    store_tree_key(
        node_path=event['node_path'],
        version=event['version'],
        group_id=event['group_id'],
        key=new_key,
        db=db
    )

    # Check if we can now decrypt pending group_key_tree_shared events
    retry_pending_group_key_tree_shared(event['group_id'], recorded_by, db)

    return VALID
```

## Complexity Analysis

| Operation | Events | Notes |
|-----------|--------|-------|
| Join (self-add via invite) | O(1) | `group_key_shared` to invite prekey |
| Tree enrollment (by admin) | O(log n) | `tree_key_shared` for path keys |
| Send message | O(1) | `group_key_tree_shared` to root |
| Removal (enrolled user) | O(log n) | Re-key path + new group key |
| Removal (not enrolled) | O(k) | Fallback to `group_key_shared` |
| Storage per user | O(log n) | Tree keys for their path |

## Prekey Rotation and Post-Compromise Security (PCS)

### How do enrolled users update their prekeys?

Short answer: **prekey rotation doesn't affect tree key distribution**.

Tree keys flow through the tree structure, not through individual prekeys:

```
Enrollment (once):
    admin → tree_key_shared → sealed to user's group_prekey

Removal re-keying (ongoing):
    admin → tree_key_shared → encrypted to SIBLING TREE KEY
                              (not to individual prekeys)
```

The user's `group_prekey` is only used at enrollment time. After that, they receive tree key updates via sibling encryption - no prekey involved.

### What prekey rotation does affect

1. **`group_key_shared` fallback** - for non-enrolled users, needs current prekey
2. **Initial tree enrollment** - admin needs user's current prekey to share path keys
3. **Other key sharing** (device linking, etc.) - existing mechanisms

### PCS for tree keys

If Alice's device is compromised, the attacker has her tree path keys. For post-compromise security, Alice needs to rotate her tree keys - this is an O(log n) "self-update":

```python
def update_own_tree_keys(user_id, group_id, peer_id, t_ms, db):
    """PCS update - rotate own tree path keys.

    Similar to removal re-keying, but the user stays in the tree.
    Generates fresh keys for entire path, encrypted to siblings.
    """
    leaf_path = get_user_leaf_path(user_id, group_id, db)

    # Re-key entire path from leaf to root
    for node_path in path_from_leaf_to_root(leaf_path):
        sibling_path = get_sibling_path(node_path)

        if sibling_path is None:
            # Root has no sibling - just generate new root key
            # and announce via tree_key_shared to our own subtree
            new_key = crypto.random_bytes(32)
            new_version = get_current_version(group_id, node_path, db) + 1

            # Encrypt to our own child (the path we just updated)
            child_path = get_child_on_path(node_path, leaf_path)
            child_key = get_tree_key(group_id, child_path, db)

            create_tree_key_shared(
                node_path=node_path,
                version=new_version,
                encrypted_key=encrypt(new_key, child_key.key),
                sibling_path=child_path,
                sibling_version=child_key.version,
                signed_by=peer_id,
                db=db
            )
            store_tree_key(node_path, new_version, group_id, new_key, db)
            continue

        if is_subtree_empty(sibling_path, group_id, db):
            continue  # No one to encrypt to

        sibling_key = get_tree_key(group_id, sibling_path, db)
        new_key = crypto.random_bytes(32)
        new_version = get_current_version(group_id, node_path, db) + 1

        create_tree_key_shared(
            node_path=node_path,
            version=new_version,
            encrypted_key=encrypt(new_key, sibling_key.key),
            sibling_path=sibling_path,
            sibling_version=sibling_key.version,
            signed_by=peer_id,
            db=db
        )

        store_tree_key(node_path, new_version, group_id, new_key, db)
```

**Complexity:** O(log n) events for a PCS update - same as removal.

### When to trigger PCS updates

Options:
1. **Periodic** - rotate tree keys on a schedule (e.g., daily)
2. **On suspicion** - user triggers manually if they suspect compromise
3. **Piggyback on group_prekey rotation** - when user rotates their group_prekey, also update tree keys

Option 3 integrates naturally with existing PCS mechanisms.

### Offline users and tree key updates

If Alice is offline when Bob is removed (or does a PCS update):
1. `tree_key_shared` events are created, encrypted to sibling keys
2. Alice syncs later, receives the events
3. Alice decrypts using her sibling tree key (which she already has)
4. Alice now has the new tree keys

No prekey involved - it all flows through the tree structure.

## Integration with Existing Forward Secrecy

The existing forward secrecy mechanism (rekey-and-purge for expired messages) works alongside this:

1. **Message expiry**: Rekey messages to clean `group_key`, distribute via `group_key_tree_shared` to current tree root
2. **User removal**: Re-key tree path, new `group_key` uses new root
3. **PCS updates**: User re-keys own path, new keys propagate via siblings
4. **Key purging**: Purge old `tree_key` versions after all dependent events are rekeyed

## Handling Multiple Removals

If U_1 and U_3 are removed in sequence:

```
Remove U_1: re-key K_01→K_01', K_0→K_0', K_root→K_root'
           encrypt to: K_00, K_1

Remove U_3: re-key K_11→K_11', K_1→K_1', K_root'→K_root''
           encrypt to: K_10, K_0'
                            ↑ uses NEW K_0' from previous removal
```

The second removal encrypts to `K_0'` (the updated key), which U_1 doesn't have. Forward secrecy is preserved across multiple removals.

## Tree Growth

When the tree fills up:

```python
def grow_tree(group_id):
    # Add new level: old root becomes left child of new root
    old_root_key = get_tree_key(group_id, "")

    # New root with old tree as left subtree
    new_root_key = random_bytes(32)
    create_tree_key(node_path="", version=0, group_id=group_id, key=new_root_key)

    # Old root is now at path "L"
    create_tree_key(node_path="L", version=old_root_key.version,
                    group_id=group_id, key=old_root_key.key)

    # Announce via tree_key_shared so all existing users learn new root
    create_tree_key_shared(
        node_path="",
        version=0,
        encrypted_key=encrypt(new_root_key, old_root_key.key),
        sibling_path="L",  # old root
        sibling_version=old_root_key.version,
    )
```

## Implementation Notes

### Path Encoding

Use "L" and "R" for left/right children:
- "" = root
- "L" = left child of root
- "LR" = right child of left child of root
- etc.

### Leaf Assignment

Options:
1. **Sequential**: Assign leaves in order of joining (simple, predictable)
2. **Hash-based**: `leaf_path = hash(user_id)[:log2(tree_size)]` (distributed)

Sequential is simpler for initial implementation.

### Sibling Path Calculation

```python
def get_sibling_path(node_path):
    if not node_path:
        return None  # root has no sibling
    parent = node_path[:-1]
    last = node_path[-1]
    return parent + ('R' if last == 'L' else 'L')
```

### Version Tracking

Need a table to track current version of each tree node:

```sql
CREATE TABLE tree_key_versions (
    group_id TEXT,
    node_path TEXT,
    current_version INTEGER,
    PRIMARY KEY (group_id, node_path)
);
```

## Sparse Trees: What Happens When Users Leave?

When a user is removed, their leaf slot becomes empty. Over time with joins and leaves, the tree becomes sparse.

### Option A: Leave Gaps (Recommended)

When U_1 leaves, their leaf slot stays empty. No reorganization.

```
Before:                    After U_1 removed:
      K_root                     K_root'
      /    \                     /    \
    K_0    K_1                 K_0'   K_1
    / \    / \                 / \    / \
  K_00 K_01 K_10 K_11        K_00 [x] K_10 K_11
   |    |    |    |           |        |    |
  U_0  U_1  U_2  U_3         U_0      U_2  U_3
```

**Pros:**
- Simple - no reorganization needed
- Removal stays O(log n)
- Deterministic - all peers agree on tree structure

**Cons:**
- Tree becomes sparse over time
- Wasted storage for empty subtree keys
- Tree depth doesn't shrink even if most users leave

**Mitigation:** Reuse empty slots for new joins (see below).

### Option B: Compact on Removal

Move rightmost user into vacated slot, re-key their old and new paths.

**Pros:**
- Tree stays dense

**Cons:**
- Removal becomes O(2 log n) - re-key removed user's path AND moved user's path
- Complex coordination - which user moves?
- Non-deterministic without consensus on "rightmost"

**Not recommended** - adds complexity for marginal benefit.

### Option C: Periodic Rebalancing

Let tree become sparse, periodically rebuild.

**Pros:**
- Simple day-to-day operations
- Optimal tree structure after rebuild

**Cons:**
- Rebuild is O(n log n) - must re-share all keys to all users
- Requires coordination on when to rebuild
- During rebuild, which tree structure is authoritative?

**Not recommended** - rebuild cost is prohibitive.

### Recommended Approach: Leave Gaps + Slot Reuse

1. **On removal**: Leave the slot empty, re-key the path (O(log n))
2. **On join**: Prefer empty slots over growing the tree
3. **Track empty slots**: Maintain a list of available leaf positions

```python
def assign_leaf(user_id, group_id, db):
    # First, try to reuse an empty slot
    empty_slot = db.query_one(
        "SELECT leaf_path FROM empty_tree_slots WHERE group_id = ? LIMIT 1",
        (group_id,)
    )
    if empty_slot:
        db.execute(
            "DELETE FROM empty_tree_slots WHERE group_id = ? AND leaf_path = ?",
            (group_id, empty_slot['leaf_path'])
        )
        return empty_slot['leaf_path']

    # No empty slots - assign next sequential leaf (may grow tree)
    return assign_next_leaf(group_id, db)

def on_user_removed(user_id, group_id, leaf_path, db):
    # Mark slot as available for reuse
    db.execute(
        "INSERT INTO empty_tree_slots (group_id, leaf_path) VALUES (?, ?)",
        (group_id, leaf_path)
    )
```

**Why this works:**
- Groups that churn (many joins/leaves) naturally reuse slots
- Growing groups fill slots sequentially
- Shrinking groups accumulate empty slots but don't waste re-key operations
- Tree depth is `ceil(log2(max_concurrent_members))`, not `log2(total_ever_joined)`

### Empty Subtrees Optimization

When an entire subtree becomes empty, we can skip re-keying through it:

```
      K_root
      /    \
    K_0    K_1    ← If K_1 subtree is completely empty...
    / \    / \
  K_00 [x] [x] [x]
   |
  U_0

Remove U_0: Only need to re-key K_00, K_0, K_root
           No need to encrypt to K_1 (no one there to receive it)
```

But wait - this breaks the invariant! If we don't encrypt K_root' to K_1, then when a new user joins in the K_1 subtree, they can't learn K_root'.

**Solution:** When joining into an empty subtree, the joiner receives the current root key directly (sealed to their invite prekey), plus fresh keys for their path. The empty subtree's internal keys are generated fresh.

```python
def join_into_empty_subtree(user_id, group_id, leaf_path, invite_prekey_id, db):
    # Generate fresh keys for the entire path from leaf to first non-empty ancestor
    empty_prefix = find_empty_prefix(leaf_path, group_id, db)

    for node_path in path_from(empty_prefix, leaf_path):
        new_key = random_bytes(32)
        create_tree_key(node_path, version=0, group_id, new_key)
        # Share to joiner's invite prekey
        share_tree_key_to_invite(node_path, version=0, group_id, invite_prekey_id)

    # Also share current root key (so joiner can decrypt group messages)
    root_key = get_tree_key(group_id, "")
    share_tree_key_to_invite("", root_key.version, group_id, invite_prekey_id)
```

## Database Schema

### New Tables

```sql
-- Tree keys (local-only, stores actual key material)
CREATE TABLE IF NOT EXISTS tree_keys (
    tree_key_id TEXT PRIMARY KEY,      -- hash(group_id || node_path || version || key)
    group_id TEXT NOT NULL,
    node_path TEXT NOT NULL,           -- "" for root, "L", "R", "LL", "LR", etc.
    version INTEGER NOT NULL,
    key BLOB NOT NULL,                 -- 32-byte symmetric key
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    UNIQUE(group_id, node_path, version, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_tree_keys_lookup
ON tree_keys(group_id, node_path, recorded_by);

-- Tree leaf assignments (tracks which user is at which leaf)
CREATE TABLE IF NOT EXISTS tree_leaf_assignments (
    assignment_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    leaf_path TEXT NOT NULL,
    assigned_by TEXT NOT NULL,         -- peer_shared_id of admin who assigned
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    UNIQUE(group_id, user_id, recorded_by),
    UNIQUE(group_id, leaf_path, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_tree_leaf_assignments_user
ON tree_leaf_assignments(group_id, user_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_tree_leaf_assignments_leaf
ON tree_leaf_assignments(group_id, leaf_path, recorded_by);

-- Empty tree slots (available for reuse)
CREATE TABLE IF NOT EXISTS empty_tree_slots (
    group_id TEXT NOT NULL,
    leaf_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY(group_id, leaf_path, recorded_by)
);

-- Tree metadata (tracks tree size, next leaf index)
CREATE TABLE IF NOT EXISTS tree_metadata (
    group_id TEXT NOT NULL,
    next_leaf_index INTEGER NOT NULL DEFAULT 0,  -- for sequential assignment
    tree_depth INTEGER NOT NULL DEFAULT 0,       -- current tree depth
    recorded_by TEXT NOT NULL,
    PRIMARY KEY(group_id, recorded_by)
);
```

### Modified Tables

```sql
-- Add tree enrollment tracking to group_members or users
-- Option: Add column to existing table
ALTER TABLE group_members ADD COLUMN tree_enrolled INTEGER DEFAULT 0;

-- Or track via tree_leaf_assignments table (preferred - no schema change)
```

## Event Module Structure

### events/group/tree_key.py (local-only)

```python
EVENT_TYPE = 'tree_key'
SHAREABLE = False  # Local-only key material
EPHEMERAL = False
PROJECTION_TABLE = ('tree_keys', 'tree_key_id')

def create(group_id: str, node_path: str, version: int,
           key: bytes, peer_id: str, t_ms: int, db) -> str:
    """Create local tree_key event."""
    ...

def get(group_id: str, node_path: str, version: int,
        peer_id: str, db) -> dict | None:
    """Get tree key by path and version."""
    ...

def get_current(group_id: str, node_path: str, peer_id: str, db) -> dict | None:
    """Get latest version of tree key at path."""
    ...

def get_current_version(group_id: str, node_path: str, peer_id: str, db) -> int:
    """Get current version number for a node."""
    ...
```

### events/group/tree_key_shared.py (shareable)

```python
EVENT_TYPE = 'tree_key_shared'
SHAREABLE = True
EPHEMERAL = False
PROJECTION_TABLE = ('tree_keys_shared', 'tree_key_shared_id')

def create(group_id: str, node_path: str, version: int,
           encrypted_key: bytes, sibling_path: str, sibling_version: int,
           peer_id: str, peer_shared_id: str, t_ms: int, db) -> str:
    """Create tree_key_shared event (re-keying announcement)."""
    ...

def create_to_prekey(group_id: str, node_path: str, version: int,
                     key: bytes, recipient_prekey: dict,
                     peer_id: str, peer_shared_id: str, t_ms: int, db) -> str:
    """Create tree_key_shared sealed to recipient's prekey (for enrollment)."""
    ...

def project(tree_key_shared_id: str, recorded_by: str,
            recorded_at: int, db) -> str | None:
    """Project tree_key_shared - decrypt and store if we can."""
    ...
```

### events/group/tree_leaf_assignment.py (shareable)

```python
EVENT_TYPE = 'tree_leaf_assignment'
SHAREABLE = True
EPHEMERAL = False
PROJECTION_TABLE = ('tree_leaf_assignments', 'assignment_id')

def create(group_id: str, user_id: str, leaf_path: str,
           peer_id: str, peer_shared_id: str, t_ms: int, db) -> str:
    """Create tree_leaf_assignment event."""
    ...

def get_user_leaf(group_id: str, user_id: str, peer_id: str, db) -> str | None:
    """Get leaf path for a user."""
    ...

def get_leaf_user(group_id: str, leaf_path: str, peer_id: str, db) -> str | None:
    """Get user at a leaf path."""
    ...

def project(assignment_id: str, recorded_by: str,
            recorded_at: int, db) -> str | None:
    """Project tree_leaf_assignment."""
    ...
```

### events/group/group_key_tree_shared.py (shareable)

```python
EVENT_TYPE = 'group_key_tree_shared'
SHAREABLE = True
EPHEMERAL = False
PROJECTION_TABLE = ('group_keys_tree_shared', 'group_key_tree_shared_id')

def create(key_id: str, group_id: str, encrypted_key: bytes,
           root_version: int, peer_id: str, peer_shared_id: str,
           t_ms: int, db) -> str:
    """Create group_key_tree_shared event (group key wrapped to tree root)."""
    ...

def project(group_key_tree_shared_id: str, recorded_by: str,
            recorded_at: int, db) -> str | None:
    """Project group_key_tree_shared - decrypt group key using root tree key."""
    ...
```

## Helper Functions

### events/group/tree_utils.py

```python
def get_sibling_path(node_path: str) -> str | None:
    """Get sibling path. Returns None for root."""
    if not node_path:
        return None
    parent = node_path[:-1]
    last = node_path[-1]
    return parent + ('R' if last == 'L' else 'L')

def get_parent_path(node_path: str) -> str | None:
    """Get parent path. Returns None for root."""
    if not node_path:
        return None
    return node_path[:-1]

def path_from_leaf_to_root(leaf_path: str) -> list[str]:
    """Get all node paths from leaf to root (inclusive)."""
    paths = [leaf_path]
    current = leaf_path
    while current:
        current = get_parent_path(current)
        if current is not None:
            paths.append(current)
        else:
            paths.append("")  # root
            break
    return paths

def leaf_index_to_path(index: int, depth: int) -> str:
    """Convert leaf index to path string.

    Example: index=0, depth=2 -> "LL"
             index=1, depth=2 -> "LR"
             index=2, depth=2 -> "RL"
             index=3, depth=2 -> "RR"
    """
    if depth == 0:
        return ""
    path = ""
    for i in range(depth - 1, -1, -1):
        if (index >> i) & 1:
            path += "R"
        else:
            path += "L"
    return path

def path_to_leaf_index(path: str) -> int:
    """Convert path string to leaf index."""
    index = 0
    for char in path:
        index = index << 1
        if char == 'R':
            index |= 1
    return index

def is_subtree_empty(subtree_root: str, group_id: str, peer_id: str, db) -> bool:
    """Check if a subtree has no assigned users."""
    # Query tree_leaf_assignments for any leaves under this path
    safedb = create_safe_db(db, recorded_by=peer_id)
    result = safedb.query_one(
        """SELECT 1 FROM tree_leaf_assignments
           WHERE group_id = ? AND leaf_path LIKE ? AND recorded_by = ?
           LIMIT 1""",
        (group_id, subtree_root + '%', peer_id)
    )
    return result is None

def assign_leaf(group_id: str, peer_id: str, db) -> str:
    """Assign next available leaf path."""
    safedb = create_safe_db(db, recorded_by=peer_id)

    # First try to reuse empty slot
    empty = safedb.query_one(
        "SELECT leaf_path FROM empty_tree_slots WHERE group_id = ? AND recorded_by = ? LIMIT 1",
        (group_id, peer_id)
    )
    if empty:
        safedb.execute(
            "DELETE FROM empty_tree_slots WHERE group_id = ? AND leaf_path = ? AND recorded_by = ?",
            (group_id, empty['leaf_path'], peer_id)
        )
        return empty['leaf_path']

    # Get or create tree metadata
    meta = safedb.query_one(
        "SELECT next_leaf_index, tree_depth FROM tree_metadata WHERE group_id = ? AND recorded_by = ?",
        (group_id, peer_id)
    )

    if meta is None:
        # First user - create tree with depth 1
        safedb.execute(
            "INSERT INTO tree_metadata (group_id, next_leaf_index, tree_depth, recorded_by) VALUES (?, 1, 1, ?)",
            (group_id, peer_id)
        )
        return "L"  # First leaf

    next_index = meta['next_leaf_index']
    depth = meta['tree_depth']
    max_leaves = 2 ** depth

    # Check if we need to grow the tree
    if next_index >= max_leaves:
        depth += 1
        # Note: grow_tree() handles re-keying, called separately

    leaf_path = leaf_index_to_path(next_index, depth)

    # Update metadata
    safedb.execute(
        "UPDATE tree_metadata SET next_leaf_index = ?, tree_depth = ? WHERE group_id = ? AND recorded_by = ?",
        (next_index + 1, depth, group_id, peer_id)
    )

    return leaf_path
```

## Open Questions

1. **Concurrent removals**: What if two admins remove different users simultaneously? (Version conflicts on shared ancestors)
2. **Recovery**: How do users catch up if they miss re-key events? (Need to fetch from peers who have the keys)
3. **Consistency**: How do all peers agree on tree structure? (Need deterministic slot assignment based on event ordering)
