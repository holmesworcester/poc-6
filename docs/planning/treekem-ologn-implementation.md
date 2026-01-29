# Incremental Implementation Plan for TreeKEM-style O(log n) Messaging

This document describes the implementation plan for "sender-subjective key selection with efficient removal" - a maximally simple, decentralized TreeKEM-style approach to achieve O(log n) messaging.

## Overview

By "sender subjective" we mean that each sender is responsible for tracking who is a member, who is removed, and picking keys that cover the correct membership set from the optimal combination of TreeKEM keys and leaf node public keys.

In a nutshell, you can think of this as a "sender keys" approach where senders wrap a key to every member, except that:

1. Members are constantly posting TreeKEM updates that tend—over time—to offer one key to reach many users (for sending keys efficiently) and small combinations of keys that reach many users **except** a desired subset of excluded users (for sending efficiently after a removal).
2. Senders choose whatever combination of these "reach many" keys and per-recipient keys is the most efficient for reaching everyone.

**Note:** this is intended as an attempt to determine the difficulty of achieving O(log n) scaling in a from-scratch implementation of something like the [Quiet Protocol Draft](https://hackmd.io/lXoX3VAzTU-eLoB9BTupwg) and not as a final design.

---

## Open Design Notes

### Commutative Merging Crypto

TODO: Add notes about commutative merging crypto approaches (BLS vs DH).

Keyhive implies that they would have used BLS if a good Rust library was readily available, and our advisor confirms that BLS would not raise eyebrows if used correctly. If we have access to a BLS library in the stack we decide on, we should consider doing the merging of updates in the TreeKEM way not the Keyhive way.

### Tree as Source of Truth vs. Performance Optimization

TODO: Add notes about whether the tree structure serves as the source of truth for membership, or purely as a performance optimization layered on top of existing membership CRDTs.

---

## Phase 1: Baseline Correctness and Healing with O(n) Key Broadcast and Key Request Backstop

### 1.1 Pubkey Update Job

Create a pubkey update job that creates a local-only `treekem_secret` event and corresponding, derived, shared `treekem_pubkey` for each peer (**leaf only**). Make it manually triggered for now and assume a smart trigger; we will discuss triggers later.

### 1.2 O(n)-per-message Key Broadcast

Start with O(n)-per-message key broadcast:
- Create a local-only `secret` event
- Then create a **deterministic, unsigned** `secret_shared` event wrapped to each peer's latest `treekem_pubkey` before each message is sent
- Use a fresh `secret` for each message (crude baseline)

**Key properties:**
- Deterministic events are not signed; their `event_id` is `H(canonical_event_bytes)` where `canonical_event_bytes` is the canonical encoding of the event (including the key hint + ciphertext).
- On projection, decrypting a `secret_shared` deterministically recreates the local-only `secret` keyed by that same `event_id`, so `secret_id` can be used as a hint and for blocking/unblocking.

### 1.3 Removal Epoch Event

Add a `removal_epoch` event that depends transitively on all previously-seen removals.

### 1.4 Key Events Reference Removal Epoch

Make all keying events (`secret_shared`, `treekem_pubkey`, and TreeKEM update events later) reference the latest `removal_epoch` (hard exclusion boundary).

### 1.5 Key Request Mechanism

Add `key_request` event with the rule: removed users cannot request keys; more generally, removed users cannot author keying events/messages.

**Note on key requests:** All key agreement designs have cases where under partitions the actor circulating keys will be unaware of some member devices and recipients will be missing a key; key requests and responses serves as our catch-all to cover such cases. This also lets you speculatively omit long-inactive users from key broadcast without permanent damage to their view of the network.

### Phase 1 Checkpoint

Stop here and test various concurrent removal scenarios and ensure inclusion/exclusion. Make sure it's working well in the O(n) per-message case.

---

## Phase 2: Add TreeKEM UpdatePath (Hash-Trie)

### 2.1 TreeKEM Update Operation

Add a TreeKEM-style update operation (still manually triggered—we will address triggering later). When a peer decides to update, it should:
- Create a local-only `treekem_secret` and a derived, shared `treekem_pubkey` for each node on **its own leaf→root path** in the binary-trie (hash-trie over stable `peer_id`), up to depth 20.

### 2.2 Path Dependency Chain

Each `treekem_pubkey` should depend on the previous one in the path (it refs the previous `treekem_pubkey_id`), so the path is a dependency chain.

### 2.3 TreeKEM Update Commit Event

Emit a **signed** `treekem_update` commit event that:
- references its **author `peer_id`**,
- references the current `removal_epoch`,
- references a `base_treekem_update_id` (the winning tree state it is extending / building on), and
- depends on the final `treekem_pubkey` event (so it transitively depends on the whole path).

### 2.4 Secret Sharing to Copath Nodes

For each depth on that path, emit exactly one `treekem_secret_shared` event that encrypts that depth's path secret to the **copath node pubkey** from the referenced base/winning tree state (one ciphertext to the copath node pubkey, not a ciphertext per member).

**Note:** If a copath node pubkey for a given depth is not available in the sender's current view, the update simply does not "serve" that copath subtree at that depth yet; those members will rely on **leaf fallback at message send** (or later key requests) until they participate in updates and become represented in the winning tree.

### 2.5 Conflict Resolution

When there are multiple conflicting `treekem_update` commits, choose the winning update by the lowest `treekem_update_id`, and apply the entire winning update path as a unit (rather than picking winners node-by-node).

### Phase 2 Checkpoint

Make sure all tests are passing (including concurrent removals and concurrent updates).

---

## Phase 3: O(log n) Removals and Leaf Fallback Only on Message Send

### 3.1 What Senders Do Immediately After a Removal

When a sender observes a new `removal_epoch` (i.e., a removal they accept):

1. They **must stop using any tree/message keys that could be known to the removed peer**. Concretely: new sends must be keyed to the latest `removal_epoch`.

2. On the **first send after removal**, the sender will:
   - Emit a `treekem_update` under the new `removal_epoch` (O(log n)) — this update may re-use keys/subtrees from the prior winning tree state only where the removed user(s) were not members of that subtree
   - Broadcast the content key to the newly-updated tree *and* leaf-wrap it to non-removed recipients who are excluded in the new tree state (bounded by an inactivity limit), and rely on `key_request` healing for inactive recipients.

### 3.2 Sending with Tree + Leaf Fallback Policy (No member_epoch)

1. On send, the sender selects a recipient set based on their local membership view (e.g., "group members minus removed", plus any active invite-link keys if you support history via invite links).

2. The sender encrypts `secret_shared` to a **tree cover** derived from the current **winning** `treekem_update` state (node `treekem_pubkey`s), and then optionally adds **leaf-wrapped** `secret_shared` entries for uncovered recipients, subject to an inactivity limit.

3. **Leaf fallback policy:** treat a member as "covered by the tree" if their `peer_id` appears as an **author of an update included in the current winning tree** (e.g., in the winning chain since the last accepted removal epoch). Leaf-wrap to authorized members whose `peer_id` is not represented in that winning tree view, up to a configured inactivity limit. Members beyond that limit rely on `key_request` healing when they return.

### 3.3 Resulting Complexity

Now the system is:
- **O(1)** per message for the tree-served active set (reuse + tree cover),
- **O(log n)** for updates and removals (UpdatePath),
- and **leaf fallback happens only at message send** under a simple, deterministic policy ("not represented in the winning tree").

### Phase 3 Checkpoint

Test various concurrent removal scenarios and ensure inclusion/exclusion.

---

## Key Takeaways

Given a system where senders have a subjective view of network membership, as described in the [draft design](https://hackmd.io/lXoX3VAzTU-eLoB9BTupwg), and that choosing appropriate keys is straightforward since keys are labeled with the users they reach and don't reach, the tree update process is the big addition necessary for O(log n) messaging.

Since this seems fairly straightforward too, the hard part seems to be choosing dynamic behaviors that work well for real-world networks, which requires a lot of modeling (and confidence in the models) or realistic simulation.

---

## Open Questions for Dynamic Behavior Tuning

1. **Inactivity threshold:** What is the optimal threshold for excluding an inactive user and requiring that they request missing keys when they return online? (In large communities, it becomes more and more likely that some online user will know the keys you need and respond quickly.)

2. **Key request batching:** What is the optimal batching strategy for responses to key requests? How long should responses stick around for?

3. **Update triggers:** What is the optimal trigger for posting new tree updates (and the optimal number of separate maintained forks?) so that there is always likely to be a new key ready to transition to that already covers most members?

4. **New client updates:** When should new clients post their first tree update? (It's helpful if they sync a complete view of network membership and the existing tree first, but when should they be confident that they have?)

---

## Notes on BLS vs DH

Keyhive implies that they would have used BLS if a good Rust library was readily available, and our advisor confirms that BLS would not raise eyebrows if used correctly. If we have access to a BLS library in the stack we decide on, we should consider doing the merging of updates in the TreeKEM way not the Keyhive way.

---

## Further Reading

- [A Deep Dive Explainer on BeeKEM Protocol](https://meri.garden/a-deep-dive-explainer-on-beekem-protocol)
- [Amigo - Spacelab CCNY](https://github.com/spacelab-ccny/amigo)
- [Quiet Protocol Draft](https://hackmd.io/lXoX3VAzTU-eLoB9BTupwg)

---

## New Event Types Required

| Event Type | Shareable | Signed | Description |
|------------|-----------|--------|-------------|
| `treekem_secret` | No (local-only) | No | Local secret for a tree node |
| `treekem_pubkey` | Yes | No | Derived pubkey for tree node, refs previous in path |
| `treekem_update` | Yes | Yes | Commit event referencing removal_epoch and base state |
| `treekem_secret_shared` | Yes | No (deterministic) | Encrypts path secret to copath node pubkey |
| `removal_epoch` | Yes | Yes | Depends on all previously-seen removals |
| `secret` | No (local-only) | No | Per-message content encryption key |
| `secret_shared` | Yes | No (deterministic) | Message key wrapped to tree/leaf pubkeys |
| `key_request` | Yes | Yes | Request for missing keys (removed users blocked) |
