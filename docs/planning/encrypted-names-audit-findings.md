# Encrypted Names Audit Findings

**Date:** 2025-12-01
**Branch:** principled-name-updates-option-a
**Status:** Option A - Minimal Fix (Align docs with current behavior)

## Executive Summary

This audit examined the current implementation of encrypted names (usernames and device names) in the codebase to determine alignment with architectural documentation. The findings reveal that the actual implementation differs from the documented architecture in key areas, particularly around device names.

## Key Findings

### 1. Device Names Are Immutable

**Finding:** Device names are set once at peer_shared creation and cannot be updated.

**Evidence:**
- `events/identity/peer_shared.py` (line 23): `device_name` parameter in `create()` function
- `events/identity/peer_shared.py` (line 50): `device_name` is embedded in the peer_shared event
- `events/identity/peer_shared.sql` (line 8): `device_name TEXT` column in peers_shared table
- `events/identity/peer_shared.py` (line 156): device_name stored during projection

**Implementation:**
```python
def create(peer_id: str, t_ms: int, db: Any,
           invite_id: str,
           invite_private_key: bytes,
           device_name: str = "Device") -> str:
    # ...
    event_data = {
        'type': 'peer_shared',
        'public_key': crypto.b64encode(public_key),
        'peer_id': peer_id,
        'device_name': device_name,  # Set at creation time
        'created_at': t_ms
    }
```

**Rationale:** Device names are identity metadata, not user-facing display names. They are:
- Set when the device joins or links
- Stored in the immutable peer_shared event
- Used for device identification within multi-device setups
- Not encrypted (part of public peer identity)

### 2. No peer_name_update Event Type Exists

**Finding:** The codebase does NOT implement `peer_name_update` events.

**Evidence:**
- No `peer_name_update.py` file exists in `events/identity/` or any subdirectory
- Grep search confirms no references to `peer_name_update` except in documentation
- Event registry does not include peer_name_update

**Code Search Results:**
```bash
$ grep -r "peer_name_update" events/
# No results (only found in docs/)
```

**Conclusion:** The documented peer_name_update feature was never implemented. Device names remain immutable as part of peer_shared events.

### 3. Messages Do NOT Currently Enforce Username Dependency

**Finding:** Message events do not currently require or validate username_update dependencies.

**Evidence:**
- `events/content/message.py` contains no `depends_on` field checking
- No validation logic requires messages to depend on username_update events
- Messages can be created without proving sender has a username

**Impact:**
- For MVP: Acceptable - usernames are still created during join flow
- For production: May need to enforce username dependency to ensure all messages have identified senders
- Current behavior: Messages assume sender user_id exists, but don't block on username availability

**Note:** The architecture plan documents this as a future requirement, but it's not currently enforced in validation logic.

### 4. Usernames ARE Updateable

**Finding:** Username updates are fully implemented and working as documented.

**Evidence:**
- `events/identity/username_update.py` exists and is functional
- `username_update.create()` allows creating new username events
- LWW (last-writer-wins) via global_count is implemented
- Pending decrypts table exists for key-missing scenarios

**Implementation Status:** COMPLETE

### 5. Network Names (Not Examined in Detail)

**Status:** Not audited in this review. Assumed to follow similar pattern to usernames (updateable via network_name_update events).

## Discrepancies Between Docs and Code

### Documented but Not Implemented:
1. **peer_name_update events** - Documentation describes these extensively, but they don't exist in code
2. **Message username dependency** - Documentation shows messages requiring username_update in depends_on, but validation doesn't enforce this

### Implemented but Unclear in Docs:
1. **Device name immutability** - Code clearly shows device_name as immutable, but some docs suggest it might be updateable

## Recommendations for Option A (Minimal Fix)

### 1. Update Architecture Documentation

**File:** `docs/planning/encrypted-usernames-identity-architecture-plan.md`

**Changes:**
- Remove all references to `peer_name_update` as a future feature
- Clarify that device names are immutable (set at peer_shared creation)
- Note that device_name is stored in `peers_shared.device_name` column
- Add note that messages don't currently enforce username dependency (acceptable for MVP)
- Keep username_update as-is (already correct)
- Keep network_name_update as-is (assumed correct)

### 2. Move Superseded Design Documents

**Action:** Move `docs/archive/encrypted_usernames_design.md` to clearly mark as superseded/rejected

**Reason:** This document describes a hard dependency model that was not the final implementation. It's already in archive/ but should be clearly marked as superseded.

### 3. No Code Changes Required

**Rationale:** Option A is documentation-only. Code is working as designed, we're just aligning docs with reality.

## Architectural Implications

### Why Device Names Are Immutable (Good Design)

**Advantages:**
1. **Simpler event model** - No need for device name update events
2. **Clearer identity** - Device identity is stable over time
3. **No sync complexity** - No need to handle device name conflicts or LWW resolution
4. **Appropriate semantics** - Device names are "what device is this" not "what should I call this device"

**Use Case:**
- Device names are for internal identification: "iPhone", "Desktop", "Server"
- Not user-facing display names
- Typically set once during device setup
- Rarely changed in practice

### Why Message Username Dependency Isn't Critical for MVP

**Current Behavior:**
- Users are created with username during join flow
- Messages reference user_id (which exists)
- Username might not be decrypted yet, but will be eventually

**MVP Acceptable Because:**
1. Join flow ensures username_update created immediately
2. Normal case: username decrypted before first message
3. Edge case: If username not decrypted, message still valid (user exists)
4. UI can show placeholder until username arrives

**Future Enhancement:**
- Enforce depends_on=[username_update_id] in message validation
- Blocks messages until sender's username is decryptable
- Ensures all visible messages have identified authors

## Summary Table

| Feature | Documented | Implemented | Status |
|---------|-----------|-------------|---------|
| Usernames (username_update) | YES | YES | CORRECT |
| Device names (immutable) | UNCLEAR | YES | NEEDS DOC UPDATE |
| Device name updates (peer_name_update) | YES | NO | REMOVE FROM DOCS |
| Network names (network_name_update) | YES | ASSUMED | NOT AUDITED |
| Message username dependency | YES | NO | NOTE AS FUTURE |

## Action Items for Option A

1. Create this findings document
2. Update `encrypted-usernames-identity-architecture-plan.md`:
   - Remove peer_name_update references
   - Clarify device name immutability
   - Add note about message username dependency being future work
3. Move `encrypted_usernames_design.md` to archive with clear superseded status
4. Commit with message: "Docs: Align encrypted names documentation with actual implementation"

## Conclusion

The current implementation is **architecturally sound** but documentation is **out of sync**. Option A corrects this by updating docs to match code reality, with no code changes required.

Device name immutability is a **good design choice** that simplifies the system. The documented peer_name_update feature was never needed and should be removed from future plans.

Message username dependency can remain a **future enhancement** but is not critical for MVP functionality.
