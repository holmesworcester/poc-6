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

### Join Flow

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

## Open Questions

1. **Sparse trees**: What happens when users leave and create gaps? Do we compact?
2. **Concurrent removals**: What if two admins remove different users simultaneously?
3. **Recovery**: How do users catch up if they miss re-key events?
