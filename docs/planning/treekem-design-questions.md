# TreeKEM Design Questions

## Context

We're analyzing the TreeKEM implementation in `poc-6-sender-key` to understand how tree cover works for sender key distribution, and whether a simple cadence-based update model can provide efficient coverage.

## Current Understanding

### The Two-Layer Model

1. **TreeKEM Updates** share PATH SECRETS to copath pubkey owners
   - Updater creates random leaf secret, derives path to root via KDF
   - Encrypts path secrets to pubkeys at copath positions
   - Only pubkey OWNERS can decrypt and derive root

2. **Sender Key Distribution** uses the ROOT SECRET
   - If sender has root: O(1) symmetric broadcast
   - Else: O(log n) tree cover to copath pubkeys
   - Else: O(n) leaf fallback

### The Problem We're Trying to Understand

After Alice does a TreeKEM update:
```
              root
             /    \
       (0)         (1)
       /  \       /   \
    Alice Bob   Carol Dave
```

- Alice shares path secrets to her copath: pubkeys at (01) and (1)
- Bob (owns pubkey at 01) receives secret, derives Alice's root
- Carol (owns pubkey at 1) receives secret, derives Alice's root
- **Dave doesn't own any pubkey on Alice's copath → doesn't receive anything**

## Questions

### Q1: How Does Dave Get the Root?

If Dave doesn't own a pubkey on Alice's copath, how does he get Alice's root secret?

Options we've considered:
- Dave does his own update (but creates DIFFERENT root)
- Someone updates where Dave owns a copath pubkey
- Something about the base_update_id chain?

### Q2: Do Roots Converge?

Different updaters create different random leaf secrets → different roots.

If Alice's root ≠ Bob's root ≠ Carol's root, how does the system converge to a SINGLE root that everyone knows?

Is the "winning update" (lowest ID) the one whose root everyone uses? If so, how do peers who weren't on the winner's copath learn the winning root?

### Q3: What Happens with O(1) Broadcast?

Looking at the code:
```python
if root_key:
    broadcast_id = secret_broadcast.create(...)
    return [broadcast_id]  # Returns here, no fallback!
```

If sender has root and does O(1) broadcast, recipients without the root can't decrypt. There's no fallback in this path.

Is the assumption that "by the time sender has root, all recipients also have root"? How is this guaranteed?

### Q4: Tree Cover for Sender Keys

When tree cover is used (sender doesn't have root):
```python
tree_cover_pubkey_ids = treekem_update.get_tree_cover_for_sender(...)
secret_shared.share_secret_with_pubkeys(..., recipient_pubkey_ids=tree_cover_pubkey_ids)
```

This encrypts to pubkeys at copath positions. Only the OWNERS of those pubkeys can decrypt.

The code then assumes:
```python
# For now, assume tree cover covers all members if we have pubkeys
covered_members = set(other_members)
```

But this seems wrong? Tree cover only reaches pubkey owners (O(log n) peers), not all members.

### Q5: Private Key Sharing?

The design doc mentions:
> "shares the private key to non-removed neighbors at that node"

This would mean when Carol creates pubkey at (1), she shares the private key to Dave (also under (1)). Then both could decrypt.

Is this implemented? We didn't find private key sharing in the code - only path secret sharing.

### Q6: Rolling Window / TTL Model

The proposed simple model:
1. Updates have TTLs, creating a rolling window
2. Winner is selected from non-stale updates
3. Cadence ensures updates are spread across subtrees
4. Eventually everyone has the winning root

Questions:
- How does "everyone has winning root" actually happen?
- If only winner's copath has winner's root, how do others get it?
- Is there implicit propagation we're missing?

### Q7: The 100K User Case

For 100,000 users:
- O(log n) ≈ 17 operations per sender
- If tree cover only reaches 17 pubkey owners, how do other 99,983 users get the key?
- Is the answer "they all have root via prior updates"?
- What's the convergence time/bandwidth for everyone to have root?

### Q8: Removal Handling

After a removal:
1. Removed user's pubkeys are filtered
2. First sender creates new TreeKEM update
3. First sender's copath receives new path secrets

But:
- Does first sender have a usable root immediately?
- Or do they fall back to tree cover / leaf?
- How quickly can O(1) broadcast resume after removal?

## What We Think the Model Should Be

```
BACKGROUND CADENCE (staggered):
    Every peer updates periodically
    Creates pubkeys on their path
    Shares path secrets to copath pubkeys
    Copath pubkey owners derive root

SENDER KEY DISTRIBUTION:
    If I have root → O(1) broadcast
    Else → O(log n) tree cover + O(n) leaf fallback

ASSUMPTION:
    With enough updates from diverse subtrees,
    everyone is eventually on SOMEONE's copath,
    and everyone can derive the winning root.
```

But we're not clear on:
1. How "winning root" propagates to non-copath members
2. Whether the current code actually achieves this
3. What cadence/TTL values make this work efficiently

## Specific Code Questions

### In `distribute_key_to_group()`:

The O(1) path returns immediately without fallback. Is this correct? What if some recipients don't have root?

### In `get_tree_cover_for_sender()`:

Returns pubkey IDs at copath positions. Only owners can decrypt. How is this O(log n) distribution to ALL members?

### In `create_update_path()`:

Creates path secrets and shares to copath. Does the KDF derivation mean copath members can derive the SAME root as the updater? Yes, but only copath pubkey OWNERS receive the secrets.

## Summary

The core question: **How does TreeKEM achieve O(log n) or O(1) distribution to ALL members, when path secrets are only shared to O(log n) pubkey owners?**

Possible answers:
1. Private keys are shared within subtrees (not implemented?)
2. Everyone eventually owns a copath pubkey via their own updates
3. Everyone eventually derives root from some update where they're on the copath
4. There's propagation we're missing
5. The model accepts that some fallback to O(n) is necessary

We need clarity on which of these is the intended design.
