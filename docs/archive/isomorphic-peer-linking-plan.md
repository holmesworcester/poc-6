# Plan: Isomorphic Peer Linking (Align with Design Doc)

## Context

Previous work (commit `6bf63e6`) added `user_id` to `peer_self` table to fix linked device messaging. However, there's a temporary hack: `user.create()` sets `peer_self.user_id` for the first device, but per `ideal_protocol_design.md` the first device should also go through the peer linking flow.

The design doc specifies (lines 139-143):
> ### Link Peer (first and later identical)
> - `invite(mode=peer)` [signed_by: user | peer linked to that user] → `peer_shared` [signed_by: invite_id]

This means ALL devices (including the first one) should create a `peer_shared` event signed by an invite, and the invite mechanism should be responsible for establishing the peer-to-user link.

## Current State

On cli-prototype (commit `6bf63e6`):
- Tests `test_alice_links_phone_to_laptop` and `test_three_devices_all_linked` pass
- Other device linking tests still fail (8 failing tests on cli-prototype)
- `user.create()` has TEMPORARY code setting `peer_self.user_id`
- `link.create()` also sets `peer_self.user_id`

On master (poc-6):
- **Incomplete cherry-pick** with conflicts in `message.py` and `invite.py`
- Need to abort with `git reset --hard HEAD` before any work

## Goal

Make peer linking isomorphic: first device and linked devices should follow the same flow. The `peer_shared` event (signed_by=invite_id) should be responsible for establishing the peer-to-user link.

---

## Two Separate Issues Identified

**Issue 1: Group Membership Propagation (failing tests)**
- When Device 2 links to Alice's account, it doesn't automatically see Alice's existing groups
- Tests expect device 2 to have group keys to decrypt messages
- The linking flow needs to wrap group keys to the new device's prekey

**Issue 2: Isomorphic Linking (design alignment)**
- Current: `new_network()` → `user.create()` sets `peer_self.user_id` directly (TEMPORARY hack)
- Target: First device should create `invite(mode=peer)` and have `peer_shared` signed by it
- This aligns with design doc: "Link Peer (first and later identical)"

### Key Discovery: peer_shared Already Supports Invite-Based Signing

The capability already exists in `peer_shared.py`:
- `create()` accepts optional `invite_id` and `invite_private_key` parameters
- `project()` verifies invite-based signatures using `invite_pubkey`
- `project()` links peers to users via `linked_peers` table when signed by invite

**We don't need to add the capability - we just need to USE it during network creation.**

---

## Implementation Plan

### Phase 1: Pre-Implementation

1. **Abort failed cherry-pick on master**:
   ```bash
   cd /home/hwilson/poc-6
   git reset --hard HEAD
   ```

2. **Continue work on cli-prototype branch**

---

### Phase 2: Fix Group Key Propagation (Commit 1)

**Goal**: Fix failing device-linking tests by ensuring linked devices receive group keys.

#### Problem Analysis (CORRECTED per design doc)

Per `ideal_protocol_design.md` lines 175-177 and 216-217, the correct mechanism is:

1. **At invite creation**: Inviter creates a `group_prekey_shared`, includes its ID in `invite_prekey_id`, and wraps ALL existing group keys to that prekey
2. **Ongoing until invite expires**: ALL peers wrap any NEW or ROTATED group keys to all outstanding (non-expired) invite prekeys

This is NOT done at `link.join()` time - it's done at **invite creation** time and **ongoing key creation** time.

#### Additional Finding: Sync Connection Issue

Investigation revealed that `invite.create(mode='link')` already wraps group keys (lines 331-368). However, the failing tests check `group_member.is_member()` which requires `group_member` events to sync.

**The root cause**: Device 1 can't establish sync connection to device 2 because:
- Device 2's `transit_prekey_shared` is created AFTER joining
- Device 1 doesn't receive it until sync (chicken-and-egg)
- Log shows `linked=0` and `connections=0` for both devices

**The passing tests work because** they only check for group keys (which come via `group_key_shared` at invite creation time), not group membership (which requires sync).

#### Design Doc References

> "Alice creates a `group_prekey_shared` event, includes its `id` in the `invite` event (`invite_prekey_id`)... She then wraps all group keys used for the default `all_members` group in `group_key_shared` events to this `group_prekey_shared`, **and any new keys are also wrapped to all outstanding invite‑referenced `group_prekey_shared` keys** just as they are to each member's current `group_prekey_shared`."

> "The inviter **(and other peers)** can wrap existing group keys to this `group_prekey_shared`"

#### Solution

**Part A: At invite creation (`invite.create()` with mode=peer)**
1. Create a `group_prekey_shared` for the invite
2. Include its ID as `invite_prekey_id` in the invite
3. Wrap ALL existing group keys the user has access to → new `group_key_shared` events sealed to that prekey
4. Include the private `group_prekey` in the invite link data

**Part B: Ongoing key wrapping (in `group_key.create()` or similar)**
1. When creating/rotating a group key, query all outstanding (non-expired) invite prekeys
2. Wrap the new key to each invite prekey (in addition to member prekeys)

#### Implementation Steps

1. **Read** existing invite and group key code:
   - `events/identity/invite.py` - how invites are created, look for `invite_prekey_id`
   - `events/group/group_key.py` - how keys are created
   - `events/group/group_key_shared.py` - how keys are wrapped to prekeys

2. **Modify `invite.create()` (mode=peer)**:
   - Create `group_prekey_shared` for the invite
   - Query all groups the user is a member of
   - For each group, wrap current group key to the invite prekey
   - Include `invite_prekey_id` in invite event
   - Include `group_prekey` private key in invite link data

3. **Modify group key creation** (if not already done):
   - When creating new keys, also wrap to all outstanding invite prekeys
   - Query invites table for non-expired invites with `invite_prekey_id`

4. **Test** by running the 4 failing tests

#### Files to Modify
- `events/identity/invite.py` - wrap existing keys at invite creation
- `events/group/group_key.py` or `group_key_shared.py` - wrap new keys to invite prekeys

#### Files to Read First
- `events/identity/invite.py` - understand current invite creation flow
- `events/group/group_key_shared.py` - understand key wrapping API
- `events/group/group.py` - how to query group membership and keys

---

### Phase 3: Isomorphic Linking Refactor (Commit 2)

**Goal**: Make first device use same linking flow as subsequent devices.

#### Problem Analysis
Current `new_network()` flow:
1. `peer.create()` → creates peer WITHOUT invite signing
2. `user.create()` → sets `peer_self.user_id` directly (TEMPORARY hack)

Target flow (per design doc):
1. `peer.create()` → creates local peer only
2. Create `invite(mode=peer)` signed by `user_id`
3. Create `peer_shared` signed by that invite
4. `peer_shared.project()` sets `peer_self.user_id`

#### Implementation Steps

1. **Modify `new_network()`** in `events/identity/user.py`:
   - After `user.create()` returns `user_id` and `user_private_key`
   - Create `invite(mode=peer)` for first device (signed by `user_id`)
   - Re-create `peer_shared` signed by that invite
   - Let projection handle `peer_self.user_id`

2. **Remove TEMPORARY hack** from `user.create()`:
   - Delete lines 96-105 that set `peer_self.user_id` directly

3. **Verify `peer_shared.project()`**:
   - Ensure it sets `peer_self.user_id` correctly for invite-signed peer_shared
   - May need to handle the `user_id` lookup from invite → user relationship

4. **Test** all device-linking tests + network creation tests

#### Files to Modify
- `events/identity/user.py` - `new_network()` and `user.create()`
- `events/identity/peer_shared.py` - verify/fix `project()` if needed

---

## Critical Files Summary

| File | Purpose |
|------|---------|
| `events/identity/invite.py` | Phase 2: Wrap existing keys at invite creation |
| `events/group/group_key.py` | Phase 2: Wrap new keys to outstanding invite prekeys |
| `events/group/group_key_shared.py` | Phase 2: Key wrapping API |
| `events/identity/user.py` | Phase 3: Modify new_network(), remove TEMPORARY |
| `events/identity/peer_shared.py` | Phase 3: Verify invite-based projection |

---

## Success Criteria

**Phase 2 complete when**:
- All 4 failing device-linking tests pass
- `test_alice_links_phone_to_laptop` still passes
- `test_three_devices_all_linked` still passes

**Phase 3 complete when**:
- TEMPORARY hack removed from user.create()
- First device goes through invite(mode=peer) flow
- All tests still pass
- Code aligns with design doc "Link Peer (first and later identical)"
