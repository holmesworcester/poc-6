# Removal-Safe Key Selection Design

## Analysis: Not a Security Gap

### Initial Concern

The protocol specification (line 460) states:

> "When encrypting a new event, a peer MUST choose a key whose recipient set excludes every `user_id` and `peer_shared_id` present in any **accepted** `remove-user` or `remove-peer` event."

Initially this seemed like a gap because `pick_key()` doesn't explicitly check for removed users.

### Why It's Not a Gap

The key phrase is **"accepted"** - meaning removal events that the peer has received and projected. The security model is **subjective/peer-scoped**.

**If Carol hasn't received a removal event:**
- From Carol's perspective, the removed user is still a valid member
- Encrypting to them is correct behavior
- There is no security violation

**The definition of "removed"** = a user that THIS peer knows to have been removed (via projected removal event).

### Current Implementation Is Correct

The existing flow already satisfies the spec:

1. **When you project a removal** → `user_removed.project()` calls `rotate_for_removal()`
2. **Key rotation** → Creates new key, updates `groups.key_id`, shares with remaining members
3. **When you send** → `pick_key()` returns your current `key_id` (already rotated)

Each peer enforces the rule relative to their own knowledge. This is consistent with the eventual consistency model used throughout the protocol.

### No Changes Needed

The "dirty keys" approach proposed earlier adds unnecessary complexity. The current implementation correctly:

- Rotates keys when YOU learn about a removal
- Uses the rotated key for subsequent encryption
- Excludes removed users from key sharing

---

## Spec Clarification Recommendation

The spec could be clearer that the security model is peer-scoped. Consider adding:

> **Note:** "Accepted" means the peer has received and successfully projected the removal event. Each peer enforces this rule based on their own view of removal state. A peer that has not yet received a removal event will correctly use keys that include the (from their perspective, not-yet-removed) user.

This aligns with the overall eventual consistency model and prevents confusion about "global" vs "local" removal state.
