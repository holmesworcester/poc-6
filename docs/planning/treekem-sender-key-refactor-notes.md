# TreeKEM TODO: Sender Key Refactoring

## Overview

The `events/group/sender_key.py` module is a pure orchestration module living in the `events/` directory, which should contain only event modules. Additionally, there's a security bug in key reuse that doesn't check removal epochs.

This document outlines the plan to:
1. Move sender_key.py logic into the appropriate event module
2. Fix the key reuse security bug
3. Remove sender_key.py

---

## 1. Move Orchestration Logic to `secret.py`

### Rationale

`events/group/secret.py` is the natural home for sender key logic because:
- It already has authority over symmetric key material
- `secret.create()` already orchestrates (calls `key_announce.create()`)
- The "pick or create" pattern is fundamentally about secrets
- Distribution is about sharing secrets (uses `secret_shared`, `secret_broadcast`)

### Functions to Move

| Function | New Location | Notes |
|----------|--------------|-------|
| `pick_or_create_key()` | `secret.py` | Rename to `get_or_create_for_group()` |
| `get_sender_key_for_group()` | `secret.py` | Rename to `get_for_group()` |
| `distribute_key_to_group()` | `secret.py` | Rename to `distribute_to_group()` |
| `distribute_to_leaves()` | `secret.py` | Keep name or make private `_distribute_to_leaves()` |
| `get_active_members()` | `secret.py` | Keep as helper, possibly make private |
| `get_group_member_peer_ids()` | `secret.py` | Keep as helper |
| `get_all_keys_for_group()` | `secret.py` | Keep name |
| `share_all_keys_to_pubkey()` | `secret.py` | Keep name |
| `get_key_for_decryption()` | `secret.py` | Merge with existing `get_key()` or keep separate |
| `get_copath_pubkeys()` | `secret.py` | Keep as helper |

### Update Callers

After moving, update all callers to use `secret.*` instead of `sender_key.*`:

```
events/content/message.py
  - sender_key.pick_or_create_key() → secret.get_or_create_for_group()

events/identity/invite.py (if applicable)
  - sender_key.share_all_keys_to_pubkey() → secret.share_all_keys_to_pubkey()

Any other callers (search for "from events.group import sender_key")
```

### Delete sender_key.py

After all logic is moved and callers updated, remove:
- `events/group/sender_key.py`

---

## 2. Fix Key Reuse Security Bug

### The Bug

In `get_sender_key_for_group()` (lines 99-129), the query finds the most recent key for a (sender, group) pair **without checking the removal epoch**:

```python
row = safedb.query_one(
    """SELECT ka.key_id FROM key_announces ka
       INNER JOIN secrets s ON s.secret_id = ka.key_id ...
       WHERE ka.group_id = ? AND ka.signed_by = ? AND ka.recorded_by = ?
       ORDER BY ka.created_at DESC
       LIMIT 1""",
    (group_id, sender_peer_shared_id, recorded_by)
)
```

### The Problem

1. Alice creates key K1 in epoch E0 (no removals)
2. K1 is distributed to Bob, Carol, Dave
3. Dave is removed → epoch E1 created
4. Alice sends a new message
5. `pick_or_create_key()` finds K1 (no epoch check) → **reuses K1**
6. Message encrypted with K1
7. **Dave can still decrypt** because he already has K1

### The Fix

Add removal epoch comparison to `get_sender_key_for_group()`:

```python
def get_sender_key_for_group(
    group_id: str,
    sender_peer_shared_id: str,
    recorded_by: str,
    db: Any,
) -> str | None:
    """Get the sender's current key for a group.

    Only returns a key if its removal_epoch_id matches the current epoch.
    This ensures forward secrecy after member removals.
    """
    # Get current removal epoch
    current_epoch_id = removal_epoch.get_current_epoch(recorded_by, db)

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Only return key if removal_epoch matches current
    # NULL epoch matches NULL (no removals yet)
    row = safedb.query_one(
        """SELECT ka.key_id FROM key_announces ka
           INNER JOIN secrets s ON s.secret_id = ka.key_id AND s.recorded_by = ka.recorded_by
           WHERE ka.group_id = ?
             AND ka.signed_by = ?
             AND ka.recorded_by = ?
             AND (ka.removal_epoch_id IS ? OR (ka.removal_epoch_id IS NULL AND ? IS NULL))
           ORDER BY ka.created_at DESC
           LIMIT 1""",
        (group_id, sender_peer_shared_id, recorded_by, current_epoch_id, current_epoch_id)
    )

    return row['key_id'] if row else None
```

### Behavior After Fix

1. Alice creates key K1 in epoch E0
2. K1 is distributed to Bob, Carol, Dave
3. Dave is removed → epoch E1 created
4. Alice sends a new message
5. `get_sender_key_for_group()` looks for key with `removal_epoch_id = E1`
6. K1 has `removal_epoch_id = E0` → **no match**
7. `pick_or_create_key()` creates new key K2 in epoch E1
8. K2 is distributed only to Bob, Carol (active members in E1)
9. **Dave cannot decrypt** - he never receives K2

### Edge Cases to Consider

1. **NULL epochs**: Both key and current epoch are NULL (no removals ever) → should match
2. **Epoch ordering**: What if we have keys from multiple past epochs? Only current epoch matters.
3. **Key rotation frequency**: After this fix, every removal triggers new key creation for all active senders on their next message. This is correct for forward secrecy but increases key distribution traffic.

---

## 3. Implementation Order

1. **Add the epoch check first** (security fix) - can be done in sender_key.py before moving
2. **Add tests** for the security fix (removed user cannot decrypt new messages)
3. **Move functions** to secret.py one at a time
4. **Update callers** after each move
5. **Run tests** after each change
6. **Delete sender_key.py** once empty

---

## 4. Testing

### Security Fix Tests

```python
def test_removed_user_cannot_decrypt_new_messages():
    """After removal, sender creates new key that removed user doesn't have."""
    # Setup: Alice, Bob, Dave in group
    # Alice sends message M1 (all can decrypt)
    # Remove Dave
    # Alice sends message M2 (only Alice, Bob can decrypt)
    # Verify: Dave cannot decrypt M2

def test_key_reuse_within_same_epoch():
    """Keys are still reused when no removal has occurred."""
    # Alice sends M1 (creates key K1)
    # Alice sends M2 (reuses K1)
    # Verify: same key_id used for both

def test_key_created_after_removal_uses_new_epoch():
    """New keys reference the current removal epoch."""
    # Setup group, remove user, send message
    # Verify: key_announce.removal_epoch_id == current epoch
```

### Refactoring Tests

- All existing tests should pass after moving logic to secret.py
- No behavior change, only code location

---

## 5. Files Changed

```
Modified:
  events/group/secret.py          # Add orchestration functions
  events/content/message.py       # Update import/call
  events/identity/invite.py       # Update import/call (if applicable)

Deleted:
  events/group/sender_key.py      # Remove after migration

Tests:
  tests/scenario_tests/test_forward_secrecy.py  # Add removal epoch tests
```

---

## 6. Fundamental TreeKEM Issues (Codex Analysis)

Analysis of the current TreeKEM implementation revealed several fundamental gaps between the design intent and actual implementation.

### 6.1 Roots Do NOT Converge Globally

**Finding**: Different updaters create different random leaf secrets → different roots. The "winning update" concept (lowest `treekem_update_id`) exists but is **not tied to root selection**.

**Evidence**:
- `events/group/treekem_update.py:create_update_path` creates fresh random leaf secrets
- `events/group/treekem_secret.py:get_root_secret_key_data` returns the most recently recorded depth=0 secret for the local peer
- Root selection is "latest seen" per peer, not "winning update" based
- Copath recipients derive the updater's root, but peers not on the copath never receive it

**Impact**: Peers can hold different root secrets. There is no mechanism to propagate the "winning root" to non-copath members.

### 6.2 O(1) Broadcast Has No Fallback

**Finding**: When sender has root and uses O(1) broadcast, recipients without the root cannot decrypt. The code **returns immediately without fallback**.

**Evidence** (`events/group/sender_key.py:distribute_key_to_group`):
```python
if root_key:
    broadcast_id = secret_broadcast.create(...)
    return [broadcast_id]  # Returns here, no fallback!
```

**Impact**: O(1) broadcast only "reaches all members" if every member already has the root secret. The code assumes convergence but does not guarantee or verify it.

### 6.3 Private Key Sharing NOT Implemented

**Finding**: The design doc mentions "shares the private key to non-removed neighbors at that node" but this is **not implemented**.

**Evidence**:
- `events/group/treekem_pubkey.py` stores private keys locally only (`SHAREABLE=False`)
- `events/group/treekem_pubkey_shared.py` shares only the public key
- No code path sends treekem private keys to subtree members or non-removed neighbors

**Impact**: Without private key sharing, copath encryption reaches only the specific pubkey owners (O(log n) peers), not everyone under that subtree node.

### 6.4 Tree Cover Placeholder Assumption

**Finding**: `distribute_key_to_group()` sets `covered_members = set(other_members)` when tree cover is used, assuming all members are covered. This is **incorrect**.

**Evidence** (`events/group/sender_key.py`):
```python
# For now, assume tree cover covers all members if we have pubkeys
covered_members = set(other_members)
```

**Impact**: Tree cover only reaches O(log n) pubkey owners. The ~99,983 other users in a 100K group do not receive the key through this path.

### 6.5 The 100K User Problem

For 100,000 users:
- O(log n) ≈ 17 operations per sender via tree cover
- Only those 17 pubkey owners can decrypt
- The other 99,983 users have no path to receive the key
- Leaf fallback is skipped because `covered_members = all`

---

## 7. Fix Strategy Options

### Option A: Implement Private Key Sharing (Complex)

Add actual private key sharing where pubkey creators share private keys to subtree members:
- When Carol creates pubkey at position (1), she shares the private key to Dave (also under (1))
- Both can then decrypt tree cover encryptions at that position

**Pros**: Achieves true O(log n) distribution to all members
**Cons**: Complex, adds key management, attack surface for key compromise

### Option B: Add Root Propagation Mechanism (Medium)

Create explicit root propagation where winners broadcast their root:
- After determining winning update, winner explicitly distributes root to all members
- Could use leaf-level encryption for non-copath members

**Pros**: Fixes convergence, enables O(1) broadcast
**Cons**: Adds O(n) distribution after each update cycle

### Option C: Always Fall Back to Leaf Distribution (Simple)

Remove O(1) and tree cover paths entirely. Always use O(n) leaf fallback:
- Sender encrypts key to each recipient's leaf pubkey
- Guaranteed delivery to all members

**Pros**: Simple, correct, no convergence issues
**Cons**: O(n) per sender per key, expensive for large groups

### Option D: Hybrid with Verification (Recommended)

Keep tiered model but add verification and fallback:
1. O(1) broadcast **only if** sender can verify all recipients have root (flag/epoch check)
2. Tree cover to pubkey owners as optimization
3. **Always** do leaf fallback for uncovered members (don't skip)

```python
# Proposed fix in distribute_key_to_group():
covered_members = set()  # Start empty, not all

if root_key and all_have_root(group_id, ...):  # Add verification
    broadcast_id = secret_broadcast.create(...)
    covered_members = set(other_members)

if tree_cover_pubkeys:
    # Only add actual pubkey owners to covered
    for pubkey_id in tree_cover_pubkeys:
        owner = get_pubkey_owner(pubkey_id, ...)
        covered_members.add(owner)
    share_secret_with_pubkeys(...)

# ALWAYS do leaf fallback for uncovered
uncovered = set(other_members) - covered_members
if uncovered:
    distribute_to_leaves(uncovered, ...)
```

**Pros**: Correct delivery, optimizes when possible
**Cons**: Falls back to O(n) until convergence is achieved

---

## 8. Updated Implementation Plan

### Phase 1: Fix Security Bug (Immediate)
1. Add removal epoch check to `get_sender_key_for_group()` (Section 2)
2. Add tests for forward secrecy after removal
3. This is independent of TreeKEM issues

### Phase 2: Fix Tree Cover Assumption (Before Refactor)
1. Remove `covered_members = set(other_members)` placeholder
2. Track actual covered members (pubkey owners only)
3. Always do leaf fallback for uncovered members
4. This ensures correctness even without O(1) broadcast

### Phase 3: Move to secret.py (After Fixes)
1. Move functions as planned in Section 1
2. Update callers
3. Delete sender_key.py

### Phase 4: Consider Root Convergence (Future)
1. Evaluate whether O(1) broadcast is worth implementing correctly
2. If yes, implement Option D (verification + fallback)
3. If not, remove O(1) path entirely

---

## 9. Key Insight

The current code was written with an assumption that "tree cover reaches all members" but without implementing the mechanism (private key sharing) that would make this true. The fix is to either:

1. **Implement the mechanism** (private key sharing) - complex but enables O(log n)
2. **Remove the assumption** (always fall back) - simple but O(n)

For now, Option D (remove assumption, always fall back for uncovered) is the safest path forward. It ensures correctness while preserving the optimization paths for when they apply.
