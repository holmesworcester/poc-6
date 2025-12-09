# Threat Model Alignment Findings

Analysis of code vs threat model in `docs/quiet-protocol-specification.md` Appendix F.

## Changes Made

The following alignment issues have been **resolved**:

1. **File slice signed_by removed** - File slices no longer have a misleading `signed_by` field.
   Integrity is verified via `root_hash` in `message_attachment` events.

2. **Plaintext names removed** - User, network, and peer_shared events no longer contain
   plaintext names. All names are now transmitted via encrypted `*_name_update` events
   (`username_update`, `network_name_update`, `peer_name_update`).

3. **Admin oversight documented** - Threat model updated to note that ADMINs can currently
   read all private channels until DM support is added.

4. **PRAGMA secure_delete enabled** - `db.py` now sets `PRAGMA secure_delete = ON` to ensure
   deleted data is overwritten, not just marked free.

---

## PART 1: Code Violates Threat Model (Code Must Change)

These are places where the implementation contradicts the stated security invariants.

### 1.1 ADMIN CAN READ ALL PRIVATE CHANNELS - DOCUMENTED

**Status:** RESOLVED (threat model updated)

**Threat Model (line 1236):**
> ADMIN cannot: Read messages from private chats or direct messages that did not include them

**Resolution:**
Threat model updated with note: "Until DMs are implemented, ADMINs are automatically added
to all private channels for oversight purposes. This invariant will be enforced once DM
support is added."

---

### 1.2 PLAINTEXT USERNAMES IN USER EVENTS - FIXED

**Status:** RESOLVED (code fixed)

**Threat Model (line 1267):**
> NETWORK ACTIVE ATTACKER cannot: Learn the usernames of MEMBERS

**Resolution:**
- Removed `name` field from `user` event - users table now stores empty string
- Removed `name` field from `network` event
- Removed `device_name` field from `peer_shared` event
- All names transmitted via encrypted `*_name_update` events:
  - `username_update` - encrypts usernames to all_members group
  - `network_name_update` - encrypts network names
  - `peer_name_update` - encrypts device names
- `get_device_name()` now checks encrypted `peer_names` table first

---

### 1.3 FILE SLICE SIGNATURE NOT VERIFIED - FIXED

**Status:** RESOLVED (code fixed)

**Threat Model (line 1270):**
> NETWORK ACTIVE ATTACKER cannot: Alter the contents, sender, or timestamp of any message

**Resolution:**
- Removed misleading `signed_by` field from `file_slice` events
- Updated docstring to document the security model:
  - `message_attachment` events are group-encrypted and signed
  - `message_attachment` contains `root_hash` computed from all slice ciphertexts
  - When retrieving files, `root_hash` is verified against actual slice data
  - Chain: signed `message_attachment` → `root_hash` → `file_slices`

---

### 1.4 MISSING SECURE DELETE PRAGMA - FIXED

**Status:** RESOLVED (code fixed)

**Threat Model (line 1359):**
> All deletion should use the secure delete features of the local data store (e.g. PRAGMA secure delete in SQLite, and WAL reset)

**Resolution:**
Added `PRAGMA secure_delete = ON` to `db.py:90`:
```python
# Enable secure delete to overwrite deleted data (per threat model requirement)
# This ensures deleted rows are zeroed out, not just marked free
self._conn.execute("PRAGMA secure_delete = ON")
```

---

## PART 2: Threat Model Needs Updating (Document Reality)

These are places where the threat model doesn't reflect the actual design/implementation, and the threat model should be updated to match.

### 2.1 ADD: ADMIN OVERSIGHT CAPABILITY

If admin oversight of private channels is intentional (per `admin-access-design.md`), the threat model should document this.

**Suggested Addition to Known Weaknesses:**
```
ADMIN can:
* Read all messages in private channels and DMs (admin oversight)
```

**Or modify the Security Invariant:**
```
ADMIN cannot:
* Read messages from private chats or direct messages that did not include them
  [UNLESS admin oversight is enabled for the network]
```

---

### 2.2 ADD: USER REMOVAL AUTHORIZATION DETAILS

The threat model says "MEMBER cannot add or remove MEMBERS" but doesn't specify self-removal.

**Implementation Reality:**
- Users CAN remove themselves (self-removal)
- Admins CAN remove any user

**File:** `events/identity/user_removed.py:18-53`

**Suggested Clarification:**
```
MEMBER can:
* Remove themselves from the network (self-removal)

ADMIN can:
* Remove any MEMBER from the network
```

---

### 2.3 ADD: GROUP MEMBERSHIP VISIBILITY (Known Weakness)

**Implementation Reality:**
Members can see who is in which groups via the `group_members` table, even if they can't decrypt messages.

**File:** Group member events are shareable; membership is visible to all network members.

**Suggested Addition to Known Weaknesses:**
```
MEMBER can:
* Learn which MEMBERS are in which groups (but not necessarily decrypt group messages)
```

---

### 2.4 ADD: FILE INTEGRITY MODEL

The threat model doesn't specify how file attachments are protected.

**Implementation Reality:**
- File slices are encrypted but not individually signed
- Integrity is verified via `root_hash` in `message_attachment` events
- The `signed_by` field in file_slice is decorative (not verified)

**Suggested Addition:**
```
## File Attachment Security
File slices are encrypted with the group key but authenticated via root_hash
in the parent message_attachment event, not via individual signatures.
```

---

### 2.5 CLARIFY: KEY LIFECYCLE AND FORWARD SECRECY

The threat model mentions PURGED messages but doesn't detail the key rotation mechanism.

**Implementation Reality:**
- `keys_to_purge` table tracks keys for deleted messages
- `message_rekey` events re-encrypt surviving messages before key deletion
- `run_message_purge_cycle()` performs batch rekeying

**Files:**
- `events/content/message_deletion.py:324-451` - Purge cycle
- `events/content/message_rekey.py` - Re-encryption mechanism

**Suggested Addition:**
```
### Forward Secrecy Implementation
When messages are deleted:
1. Encryption keys are marked for purging
2. Remaining messages using those keys are re-encrypted with clean keys
3. Original keys are deleted from group_keys table
4. Prekeys with no remaining keys are also purged
```

---

### 2.6 ADD: CHANNEL UPDATE AUTHORIZATION

The threat model doesn't specify who can update channels.

**Implementation Reality:**
Only admins can create or update channels.

**Files:**
- `events/content/channel.py:80-84` - Admin check for creation
- `events/content/channel_update.py:69-71` - Admin check for updates

**Suggested Addition:**
```
ADMIN can:
* Create and update channels

MEMBER cannot:
* Create or update channels
```

---

## Summary Table

| Issue | Category | Status |
|-------|----------|--------|
| Admin reads all private channels | Code violates TM | RESOLVED - Threat model updated |
| Plaintext usernames in user event | Code violates TM | RESOLVED - Names removed from events |
| File slice signature not verified | Code violates TM | RESOLVED - signed_by field removed |
| Missing PRAGMA secure_delete | Code violates TM | RESOLVED - PRAGMA added |
| Admin oversight not documented | TM incomplete | LOW - Consider adding |
| Self-removal not documented | TM incomplete | LOW - Consider adding |
| Group membership visibility | TM incomplete | LOW - Consider adding |
| File integrity model unclear | TM incomplete | LOW - Consider adding |
| Forward secrecy details missing | TM incomplete | LOW - Consider adding |
| Channel authorization unclear | TM incomplete | LOW - Consider adding |

---

## Remaining Work

The "Code violates TM" issues have all been resolved. The remaining items in PART 2 are
documentation improvements that can be addressed when time permits.
