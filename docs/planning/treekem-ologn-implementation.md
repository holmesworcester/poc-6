# Incremental Implementation Plan for TreeKEM-style O(log n) Messaging

This document describes the implementation plan for "sender-subjective key selection with efficient removal" - a maximally simple, decentralized TreeKEM-style approach to achieve O(log n) messaging.

## Overview

By "sender subjective" we mean that each sender is responsible for tracking who is a member, who is removed, and picking keys that cover the correct membership set from the optimal combination of TreeKEM keys and leaf node public keys.

In a nutshell, you can think of this as a "sender keys" approach where senders wrap a key to every member, except that:

1. Members are constantly posting TreeKEM updates that tend—over time—to offer one key to reach many users (for sending keys efficiently) and small combinations of keys that reach many users **except** a desired subset of excluded users (for sending efficiently after a removal).
2. Senders choose whatever combination of these "reach many" keys and per-recipient keys is the most efficient for reaching everyone.

**Note:** this is intended as an attempt to determine the difficulty of achieving O(log n) scaling in a from-scratch implementation of something like the [Quiet Protocol Draft](https://hackmd.io/lXoX3VAzTU-eLoB9BTupwg) and not as a final design.

---

## Design Decisions

### Naming: Replacing Existing Events

This system **replaces** the existing `group_key` / `group_key_shared` / `group_prekey` system:

| Old Event | New Event | Rationale |
|-----------|-----------|-----------|
| `group_key` | `secret` | "secret" is clearer - it's a symmetric secret, not a "key" in the asymmetric sense |
| `group_key_shared` | `secret_shared` | Consistent with above |
| `group_prekey` | `pubkey` | "pubkey" is clearer than "prekey" - it's just a public key for wrapping |

### Tree Structure

- The tree is a **binary hash-trie over `peer_id`** (device identifiers, not user identifiers)
- A peer's position is determined by the first N bits of `SHA256(peer_id)`
- Maximum depth: 20 (supports ~1M peers without collision issues)
- Collision handling: if two peers share a 20-bit prefix, they share a leaf and must use leaf-level wrapping between themselves

### Tree as Performance Optimization (Not Source of Truth)

The tree is purely a **performance optimization** layered on top of existing membership CRDTs:
- Membership is authoritative in the existing `group_members` / `removed_users` / `removed_peers` tables
- The tree provides efficient key distribution but doesn't define membership
- Senders always check membership tables, then use tree for efficient wrapping

### Signing Policy

**All shareable events are signed** unless explicitly marked as deterministic:
- Signed events: `treekem_update`, `removal_epoch`, `key_request`, `pubkey`
- Deterministic (unsigned): `secret_shared`, `treekem_secret_shared`

Deterministic events derive their `event_id` from `H(canonical_event_bytes)` and are accepted only when:
1. They can be decrypted by the recipient, AND
2. They are transitively referenced by a signed event (e.g., a signed message references the `secret_id`)

### Conflict Resolution

When multiple `treekem_update` commits conflict (same depth/position, same `removal_epoch`), the winner is chosen by **lowest `treekem_update_id`** (hash-based).

**Note:** This is grindable by an attacker who searches for keys that hash lower. For this prototype, we accept this limitation. A production system should use a non-grindable tie-breaker (lamport time + author ID, or VRF).

---

## Core Concepts

### The `secret` / `secret_shared` Relationship

```
Sender creates:
  secret (local-only, contains symmetric key K)
    ↓
  secret_shared[recipient_1] (wrapped K to recipient_1's pubkey)
  secret_shared[recipient_2] (wrapped K to recipient_2's pubkey)
  ... (one per recipient)

Each recipient decrypts their secret_shared to recover K.
All recipients derive the SAME secret event (deterministic from K).
The secret_id = H(canonical_secret_bytes) is identical for all recipients.
```

This means:
- `secret_id` is stable across all recipients (unlike per-recipient `secret_shared_id`)
- Messages reference `secret_id` as the encryption key hint
- `secret_shared` events are deterministic given (secret, recipient_pubkey)

### The `removal_epoch` Event

A `removal_epoch` event captures a sender's view of all known removals at a point in time.

**Structure:**
```
removal_epoch {
  removal_refs: [removal_id, ...]  // References to peer_removed/user_removed events
  parent_epochs: [removal_epoch_id, ...]  // Previous epochs being merged/extended
  signed_by: peer_shared_id
  created_at: timestamp
}
```

**Properties:**
- **Signed** by the creating peer (only non-removed peers can create)
- **Convergence**: achieved by referencing as many graph heads as possible
- If a peer knows of removals not captured in any single epoch, they emit a new `removal_epoch` that references multiple parent epochs
- The "current" removal_epoch for a peer is the one that transitively includes all removals they've seen

**Authorization:**
- Only admins can emit `peer_removed` / `user_removed` events (existing system)
- Any non-removed peer can emit `removal_epoch` to advance the frontier

### The Hash-Trie Structure

```
                        [root: depth 0]
                       /              \
              [depth 1: 0...]      [depth 1: 1...]
              /          \          /          \
         [d2: 00...]  [d2: 01...]  [d2: 10...]  [d2: 11...]
            ...          ...          ...          ...
         [leaf: peer_A] [leaf: peer_B] ...

Position of peer_id P:
  path = first 20 bits of SHA256(P)
  path[0] determines left/right at depth 1
  path[1] determines left/right at depth 2
  ... and so on
```

Each peer "owns" all nodes on their leaf-to-root path and can update secrets for those nodes.

---

## Phase 1: Baseline Correctness with O(n) Key Broadcast

### 1.1 Pubkey Event

Create a `pubkey` event type that replaces `group_prekey`:

```
pubkey {
  public_key: bytes (X25519)
  signed_by: peer_shared_id
  created_at: timestamp
}
```

- **Signed** by the peer
- Shareable
- Peers update their pubkey periodically for forward secrecy

### 1.2 Secret and Secret_Shared Events

**`secret`** (local-only):
```
secret {
  key: bytes (32-byte symmetric key)
}
event_id = H(canonical_bytes)  // Deterministic
```

**`secret_shared`** (shareable, deterministic):
```
secret_shared {
  secret_id: bytes (hint to the secret)
  recipient_pubkey_id: bytes (which pubkey was used)
  ciphertext: bytes (secret.key wrapped to recipient's pubkey)
  removal_epoch_id: bytes (hard exclusion boundary)
}
event_id = H(canonical_bytes)  // Deterministic - same inputs = same event_id
```

### 1.3 Removal Epoch Event

```
removal_epoch {
  removal_refs: [removal_id, ...]  // Direct refs to removal events
  parent_epochs: [epoch_id, ...]   // Merge multiple epoch heads
  signed_by: peer_shared_id
  created_at: timestamp
}
```

### 1.4 Key Request / Response

**`key_request`** (signed):
```
key_request {
  requested_secret_id: bytes
  removal_epoch_id: bytes  // Proves requester knows this epoch
  signed_by: peer_shared_id
  created_at: timestamp
}
```

**Response:** A `secret_shared` event wrapped to the requester's pubkey. This is deterministic (not signed) - given the same (secret, recipient_pubkey), any responder produces the same `secret_shared`.

**Authorization:**
- Responders check that requester is not in `removed_peers` / `removed_users`
- Responders check that their `removal_epoch` doesn't include removals unknown to the requester's claimed epoch

### 1.5 O(n) Message Send Flow

1. Sender creates `secret` with fresh key K
2. For each recipient peer_id (from membership minus removed):
   - Get recipient's latest `pubkey`
   - Create `secret_shared` wrapped to that pubkey, referencing current `removal_epoch_id`
3. Create message encrypted with K, referencing `secret_id`

### Phase 1 Checkpoint

Test scenarios:
- Basic send/receive with 2-3 peers
- Removal: Alice removes Bob, sends message, verify Bob cannot decrypt
- Concurrent removal: Alice removes Bob on partition A, Charlie removes Dave on partition B, verify convergence
- Key request: Peer comes online after missing messages, requests keys, receives them

---

## Phase 2: Add TreeKEM UpdatePath (Hash-Trie)

### 2.1 TreeKEM Secret and Pubkey Events

**`treekem_secret`** (local-only):
```
treekem_secret {
  key: bytes (32-byte secret for this tree node)
  depth: int (0 = root, increasing toward leaves)
  path_prefix: bytes (the bits identifying this node's position)
}
event_id = H(canonical_bytes)
```

**`treekem_pubkey`** (shareable, **signed**):
```
treekem_pubkey {
  public_key: bytes (X25519, derived from treekem_secret)
  depth: int
  path_prefix: bytes
  parent_pubkey_id: bytes (previous node in path, for dependency chain)
  signed_by: peer_shared_id
  created_at: timestamp
}
```

### 2.2 TreeKEM Update Operation

When a peer updates their tree path:

1. Generate new `treekem_secret` for each node on leaf→root path (up to depth 20)
2. Derive `treekem_pubkey` for each, chaining dependencies (leaf refs depth-1, depth-1 refs depth-2, etc.)
3. For each node, create `treekem_secret_shared` encrypting the secret to the **copath sibling's pubkey** from the base tree state

### 2.3 TreeKEM Update Commit

**`treekem_update`** (shareable, signed):
```
treekem_update {
  author_peer_id: peer_shared_id
  removal_epoch_id: bytes
  base_update_id: bytes (the winning tree state being extended, or null for bootstrap)
  root_pubkey_id: bytes (final pubkey in the path - transitively deps on whole path)
  signed_by: peer_shared_id
  created_at: timestamp
}
```

### 2.4 Secret Sharing to Copath

**`treekem_secret_shared`** (shareable, deterministic):
```
treekem_secret_shared {
  treekem_secret_id: bytes (hint)
  depth: int
  copath_pubkey_id: bytes (the sibling node's pubkey used for wrapping)
  ciphertext: bytes
  removal_epoch_id: bytes
}
event_id = H(canonical_bytes)
```

If a copath pubkey is unavailable (peer hasn't updated yet), that depth is skipped - those members use leaf fallback.

### 2.5 Tree Cover Algorithm

To send a message using the tree:

```python
def compute_tree_cover(recipient_peer_ids, winning_tree_state, removal_epoch):
    """
    Returns: list of (node_pubkey_id, covered_peer_ids) pairs

    Algorithm:
    1. Build set of recipient positions (SHA256 prefixes)
    2. Starting from root, recursively:
       - If node pubkey exists in winning_tree AND all recipients under
         this subtree are in recipient set AND no removed peers under
         this subtree: use this node (covers whole subtree)
       - Otherwise: recurse into children
    3. Leaf fallback: recipients not covered by any tree node get
       individual secret_shared to their leaf pubkey
    """
    covered = []
    uncovered = set(recipient_peer_ids)

    def recurse(depth, prefix):
        node_pubkey = winning_tree_state.get_pubkey(depth, prefix)
        subtree_members = get_peers_under_prefix(prefix, depth)
        subtree_recipients = subtree_members & uncovered
        subtree_removed = subtree_members & get_removed_peers(removal_epoch)

        if not subtree_recipients:
            return  # No recipients here

        if node_pubkey and not subtree_removed and subtree_recipients == subtree_members & uncovered:
            # This node covers all needed recipients with no removed peers
            covered.append((node_pubkey, subtree_recipients))
            uncovered -= subtree_recipients
        else:
            # Recurse into children
            recurse(depth + 1, prefix + '0')
            recurse(depth + 1, prefix + '1')

    recurse(0, '')

    # Remaining uncovered get leaf fallback
    for peer_id in uncovered:
        leaf_pubkey = get_leaf_pubkey(peer_id)
        if leaf_pubkey:
            covered.append((leaf_pubkey, {peer_id}))

    return covered
```

### 2.6 Winning Tree Selection

The "winning" tree state at any `removal_epoch` is determined by:

1. Collect all `treekem_update` events that reference this `removal_epoch` (or an ancestor epoch)
2. For each (depth, path_prefix) position, if multiple updates, winner = lowest `treekem_update_id`
3. Build composite tree from all winners

**Bootstrap / Base Selection:** Devices sync as much as they can, then pick a base by tiebreaking among known `treekem_update` events. When no base exists (new network), `base_update_id` is null and the update starts a fresh tree.

**New Device Behavior:** The `treekem_update` job runs infrequently and only after a device has been online for some time. This ensures new devices sync a substantial view of the existing tree before posting their first update, avoiding churn from partially-informed updates.

**Cross-epoch updates:** Updates referencing an older `removal_epoch` are still valid but may be superseded by updates under the newer epoch (which can selectively reuse subtrees where no removed peers exist).

### Phase 2 Checkpoint

Test scenarios:
- Single peer updates tree path, others can derive secrets via copath
- Multiple peers update concurrently, verify deterministic winner selection
- Removal invalidates subtree containing removed peer, verify re-keying

---

## Phase 3: O(log n) Removals and Efficient Sending

### 3.1 Post-Removal Behavior

When a sender observes a new removal (via new `removal_epoch`):

1. **Stop using compromised keys**: Any tree node whose subtree contained the removed peer is now untrusted
2. **On first send**: Emit `treekem_update` under new `removal_epoch`:
   - Reuse subtrees that didn't contain the removed peer
   - Generate fresh secrets for nodes on removed peer's path
3. **Leaf fallback**: For peers not yet in the new tree, wrap to their individual `pubkey`

### 3.2 Sender's Leaf Fallback Policy

The sender's responsibility:
1. Compute recipient set from membership tables, **excluding all known removed users/peers**
2. Each `secret_shared` references the current `removal_epoch_id`
3. Use tree cover where available, leaf fallback for the rest

Since `secret_shared` depends on `removal_epoch`, a recipient who later learns of a removal they didn't know about can verify:
- If the sender's `removal_epoch` includes all removals the recipient knows → message is valid
- If sender's epoch is missing a removal → recipient should not trust the message (sender was partitioned)

### 3.3 Resulting Complexity

| Operation | Complexity |
|-----------|------------|
| Message send (active tree) | O(1) - reuse tree cover |
| Message send (with leaf fallback) | O(k) where k = uncovered recipients |
| TreeKEM update | O(log n) - one secret per depth |
| Removal | O(log n) - invalidate one path |
| Key request/response | O(1) |

---

## Wire Format Constraints

The existing system uses 512-byte fixed-size wire envelopes (48-byte header + 400-byte payload + 64-byte signature).

**Payload budget per event type:**

| Event | Key Fields | Estimated Size | Fits? |
|-------|------------|----------------|-------|
| `secret` | 32-byte key | 32 bytes | Yes |
| `secret_shared` | 32-byte secret_id + 32-byte pubkey_id + 32-byte epoch + ~80-byte ciphertext | ~180 bytes | Yes |
| `pubkey` | 32-byte pubkey | 32 bytes | Yes |
| `removal_epoch` | N×32-byte refs | Up to 12 refs (~384 bytes) | Yes (12 refs max per event) |
| `treekem_update` | 32×4 = 128 bytes | 128 bytes | Yes |
| `treekem_pubkey` | 32-byte pubkey + 32-byte parent + depth + prefix | ~70 bytes | Yes |
| `treekem_secret_shared` | Similar to secret_shared | ~180 bytes | Yes |
| `key_request` | 32-byte secret_id + 32-byte epoch | 64 bytes | Yes |

**`removal_epoch` overflow:** If more than ~12 removal refs are needed, emit multiple `removal_epoch` events that chain together via `parent_epochs`.

---

## Open Questions for Dynamic Behavior Tuning

1. **Inactivity threshold:** What is the optimal threshold for excluding an inactive user and requiring that they request missing keys when they return online?

2. **Key request batching:** What is the optimal batching strategy for responses to key requests? How long should responses stick around for?

3. **Update triggers:** What is the optimal trigger for posting new tree updates so that there is always likely to be a new key ready to transition to?

4. **New client updates:** When should new clients post their first tree update? (It's helpful if they sync a complete view first.)

---

## Notes on BLS vs DH

Keyhive implies that they would have used BLS if a good Rust library was readily available. If we have access to a BLS library, we should consider doing the merging of updates in the TreeKEM way not the Keyhive way. This would enable commutative key updates where multiple concurrent updates can be merged without conflict.

---

## Further Reading

- [A Deep Dive Explainer on BeeKEM Protocol](https://meri.garden/a-deep-dive-explainer-on-beekem-protocol)
- [Amigo - Spacelab CCNY](https://github.com/spacelab-ccny/amigo)
- [Quiet Protocol Draft](https://hackmd.io/lXoX3VAzTU-eLoB9BTupwg)

---

## Event Types Summary

| Event Type | Shareable | Signed | Deterministic | Description |
|------------|-----------|--------|---------------|-------------|
| `secret` | No | No | Yes | Local symmetric key for message encryption |
| `secret_shared` | Yes | No | Yes | Secret wrapped to recipient's pubkey |
| `pubkey` | Yes | Yes | No | Peer's public key for receiving wrapped secrets |
| `removal_epoch` | Yes | Yes | No | Captures removal frontier, refs removal events |
| `key_request` | Yes | Yes | No | Request missing secret from peers |
| `treekem_secret` | No | No | Yes | Local secret for tree node |
| `treekem_pubkey` | Yes | Yes | No | Public key for tree node, chains in path |
| `treekem_update` | Yes | Yes | No | Commit of tree path update |
| `treekem_secret_shared` | Yes | No | Yes | Tree node secret wrapped to copath sibling |
