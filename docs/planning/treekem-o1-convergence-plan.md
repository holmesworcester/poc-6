# TreeKEM O(1) Root Convergence Plan

## Problem Statement

Empirical testing shows roots do NOT converge:
```
Members with root: 10/10
Unique roots: 10
RESULT: Roots are DIFFERENT - O(1) broadcast FAILS for some recipients!
```

Each member creates their own root from their own random leaf secret. O(1) broadcast is fundamentally broken - recipients cannot decrypt.

## Current Architecture

### What Exists
1. `treekem_update` - orchestrates update paths, has `get_winning_update()` (lowest update_id wins)
2. `treekem_secret` - stores symmetric path secrets at tree positions (depth, path_prefix)
3. `treekem_secret_shared` - shares path secrets encrypted to copath pubkeys
4. `treekem_pubkey` - stores keypairs (public + private) for tree positions
5. `treekem_pubkey_shared` - shares public keys only (SHAREABLE=True)

### What's Missing (from TREEKEM_TODO.md Section 6)

1. **Roots don't converge globally** (Section 6.1)
   - Different updaters create different random leaf secrets → different roots
   - Winning update exists but is not tied to root selection
   - Root selection is "latest seen" per peer, not "winning update" based

2. **O(1) broadcast has no fallback** (Section 6.2)
   - When sender has root and uses O(1), recipients without the root cannot decrypt
   - Code returns immediately without fallback

3. **Private key sharing NOT implemented** (Section 6.3)
   - Design mentions "shares the private key to non-removed neighbors at that node"
   - `treekem_pubkey` stores private keys locally only (SHAREABLE=False)
   - No code path sends treekem private keys to subtree members
   - Without this, copath encryption reaches only the specific pubkey owners (O(log n) peers), not everyone under that subtree node

4. **Tree cover placeholder assumption** (Section 6.4)
   - Code sets `covered_members = set(other_members)` assuming all are covered
   - This is incorrect - only O(log n) pubkey owners are covered

## The Key Question

Which approach should we implement?

### Option A: Private Key Sharing

When Carol creates a pubkey at tree position (depth=1, path_prefix=0x01):
1. Carol generates keypair, stores both public and private locally
2. Carol shares PUBLIC key via `treekem_pubkey_shared` (existing)
3. **NEW**: Carol shares PRIVATE key to subtree members (Dave, who is under 0x01)
4. Now when Alice encrypts a path secret to position (depth=1, 0x01):
   - Both Carol AND Dave can decrypt (both have the private key)
   - O(log n) copath encryptions reach O(n) members in one round

**Complexity**: Need new event type `treekem_privkey_shared`, careful key management

### Option B: Iterative Root Propagation

1. Alice creates update, derives root R_A, shares to her copath
2. Carol (on Alice's copath) receives, derives R_A
3. **NEW JOB**: Carol re-shares R_A to her own copath
4. Bob (on Carol's copath) receives R_A
5. After O(log n) rounds, all members have R_A

**Complexity**: O(log² n) total messages, multiple sync rounds, needs source tracking

### Option C: Always Leaf Fallback

Remove O(1) and tree cover paths entirely. Always use O(n) leaf encryption.

**Complexity**: Simple but O(n) per sender per key

## Questions for Review

1. Is my understanding of the private key sharing gap correct? The issue is that copath pubkeys are owned by single peers, but we need subtree members to also decrypt?

2. Which approach is preferred for this PoC:
   - Private key sharing (true O(log n), complex)
   - Iterative propagation (O(log² n), multi-round, simpler)
   - Leaf fallback (O(n), simplest)

3. Are there other gaps I'm missing?

## Files Involved

| File | Current Role |
|------|--------------|
| `events/group/treekem_pubkey.py` | Creates keypairs, stores private locally |
| `events/group/treekem_pubkey_shared.py` | Shares public keys only |
| `events/group/treekem_secret.py` | Stores path secrets (no source tracking) |
| `events/group/treekem_secret_shared.py` | Encrypts to copath pubkeys |
| `events/group/treekem_update.py` | Orchestrates updates, has winning logic |
| `events/group/sender_key.py` | Distribution logic, O(1) path |

## Proposed Implementation (If Private Key Sharing)

### Phase 1: Add Private Key Sharing Events

1. Create `events/group/treekem_privkey_shared.py`:
   - When pubkey created at position X, share private key to subtree members
   - Recipients store private key, can now decrypt for that position

2. Modify `treekem_pubkey.create()`:
   - After creating pubkey, determine subtree members
   - Create `treekem_privkey_shared` events to each

### Phase 2: Track Source Update ID

1. Add `source_update_id` to `treekem_secrets` table
2. Query winning root by source_update_id
3. Only use O(1) when have winning root

### Phase 3: Fix sender_key.py

1. Remove `covered_members = set(other_members)` placeholder
2. Query winning update, use its root for O(1)
3. Always fall back for uncovered members
