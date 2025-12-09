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

#### Why inviter must assign leaf position

1. **Tree state knowledge**: Only existing members know current tree structure
2. **Empty slot reuse**: Inviter can check `empty_tree_slots` table
3. **Consistency**: All peers will see the `invite` event with `leaf_path` and agree on assignment
4. **No sync-first requirement**: Joiner receives leaf assignment in invite, doesn't need to sync tree first

#### What if invite is never redeemed?

The `leaf_path` in the invite is a **reservation**, not an assignment. Two options:

**Option A: Reserve on invite creation**
- Mark slot as reserved in `empty_tree_slots` or `tree_leaf_reservations`
- If invite expires/revoked, release reservation
- Pro: Deterministic
- Con: Long-lived invites waste slots

**Option B: Assign on invite acceptance (recommended)**
- `invite` event contains `leaf_path` as a **hint**
- `invite_accepted` projection actually assigns the leaf
- If slot was taken (race condition), find next available
- Pro: No wasted slots
- Con: Slight complexity in handling conflicts

```python
def project_invite_accepted(event, db):
    leaf_path = event['leaf_path']  # hint from invite

    # Check if slot is still available
    if is_leaf_occupied(leaf_path, group_id, db):
        # Conflict - find next available
        leaf_path = find_next_available_leaf(group_id, db)
        # Note: joiner already has keys for original path
        # They'll need additional keys for new path (handled below)

    # Assign the leaf
    create_tree_leaf_assignment(
        user_id=joiner_user_id,
        leaf_path=leaf_path,
        group_id=group_id,
        db=db
    )
```

#### Joiner's perspective

1. Click invite link (contains `invite_private_key`)
2. Create local `group_prekey` from invite private key (deterministic)
3. Sync with inviter
4. Receive and decrypt `tree_key_shared` events using invite private key
5. Project `invite_accepted` - this assigns leaf position
6. Now have O(log n) tree keys, can decrypt `group_key_tree_shared` → `group_key`

#### Do they need to sync the tree first?

**No.** The invite contains everything needed:
- `leaf_path`: their position in the tree
- `tree_key_shared` events: O(log n) keys for their path

They don't need to know the full tree structure. They only need their path's keys.

### Join Flow (pseudocode)

```python
def join_user(new_user_id, group_id, invite_prekey_id):
    # Assign leaf position (consistent hash or sequential)
    leaf_path = assign_leaf(new_user_id, group_id)

    # User needs O(log n) keys: every node on path from leaf to root
    path_prefixes = get_all_prefixes(leaf_path)  # ["", "L", "LL", "LLR"]

    for prefix in path_prefixes:
        current_key = get_tree_key(group_id, prefix)
        create_group_key_shared(
            key=current_key.key,
            key_id=current_key.id,
            group_prekey_shared_id=invite_prekey_id,
            # sealed to joiner's invite prekey
        )
```

### Removal Flow

```python
def remove_user(removed_user_id, group_id):
    leaf_path = get_user_leaf_path(removed_user_id, group_id)

    # Re-key every node on removed user's path
    path_prefixes = get_all_prefixes(leaf_path)  # root to leaf

    for node_path in path_prefixes:
        sibling_path = get_sibling_path(node_path)
        sibling_key = get_tree_key(group_id, sibling_path)

        # Generate new key for this node
        new_key = random_bytes(32)
        new_version = get_current_version(group_id, node_path) + 1

        # Announce re-key, encrypted to sibling subtree
        create_tree_key_shared(
            node_path=node_path,
            version=new_version,
            group_id=group_id,
            encrypted_key=encrypt(new_key, sibling_key.key),
            sibling_path=sibling_path,
            sibling_version=sibling_key.version,
        )

        # Store locally
        create_tree_key(
            node_path=node_path,
            version=new_version,
            group_id=group_id,
            key=new_key,
        )

    # Create new group key using new root
    new_root = get_tree_key(group_id, "")  # root = empty path
    new_group_key = create_group_key(group_id)

    create_group_key_tree_shared(
        key_id=new_group_key.id,
        group_id=group_id,
        encrypted_key=encrypt(new_group_key.key, new_root.key),
        root_version=new_root.version,
    )
```

### Projection of tree_key_shared

```python
def project_tree_key_shared(event, db):
    # Check if we can decrypt (we have the sibling key)
    sibling_key = db.get_tree_key(
        group_id=event.group_id,
        node_path=event.sibling_path,
        version=event.sibling_version
    )

    if sibling_key is None:
        # We're not in the sibling subtree - we might be the removed user
        # or we're in a different subtree that will get a different re-key message
        # Check if there's another tree_key_shared for this (node_path, version)
        # that we CAN decrypt
        return VALID  # Event is valid, we just can't use it

    # Decrypt and store
    new_key = decrypt(event.encrypted_key, sibling_key.key)

    create_tree_key(
        node_path=event.node_path,
        version=event.version,
        group_id=event.group_id,
        key=new_key,
    )

    return VALID
```

## Complexity Analysis

| Operation | Messages | Storage per user |
|-----------|----------|------------------|
| Join | O(log n) key shares | O(log n) tree keys |
| Remove | O(log n) re-key events | unchanged |
| Send message | O(1) | - |
| Total storage | - | O(n log n) |

## Integration with Existing Forward Secrecy

The existing forward secrecy mechanism (rekey-and-purge for expired messages) works alongside this:

1. **Message expiry**: Rekey messages to clean `group_key`, distribute via `group_key_tree_shared` to current tree root
2. **User removal**: Re-key tree path, new `group_key` uses new root
3. **Key purging**: Purge old `tree_key` versions after all dependent events are rekeyed

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

## Open Questions

1. **Concurrent removals**: What if two admins remove different users simultaneously? (Version conflicts on shared ancestors)
2. **Recovery**: How do users catch up if they miss re-key events? (Need to fetch from peers who have the keys)
3. **Consistency**: How do all peers agree on tree structure? (Need deterministic slot assignment)
