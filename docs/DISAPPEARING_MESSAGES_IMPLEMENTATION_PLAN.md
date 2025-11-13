# Disappearing Messages Implementation Plan

## Overview

Implement per-channel disappearing message functionality following the `ideal_protocol_design.md` specification. Messages will expire based on a `disappearing_time_ms` set at channel creation and updatable by admins. Expired messages will be purged along with their associated encryption keys (after rekeying non-deleted events).

**Status**: Planning
**Branch**: `poc-6-disappearing-messages`
**Dependent on**: Message deletion, forward secrecy purging

---

## Specification Reference

From `ideal_protocol_design.md`:

### Channels (Section: Channels, lines 384-394)

> "To create a channel, peers create `channel` events naming a `group-id`, a `channel-name`, and a `disappearing-time`. Its `event-id` is its `channel-id`."

> "All channel messages use the latest known `disappearing-time` (default 0 for permanent.) Backend generates `ttl`."

> "Only members of the admin group can create channels; `channel` events are checked for signing by an admin."

> "Admins can issue a `channel-update` to change `channel-name` or `disappearing-time`."

### Unread Counts and Read Receipts (lines 412-418)

> "`seen` events must come from members of the channel. Validation: Signer in channel; message exists with created_at_ms <= viewed_at_ms. **TTL matches channel's disappearing time**."

### Event Type: Channel (Appendix A, line 648)

```
| **channel** | 0x01 | group_id 16 · channel_name 32 · disappearing_time_ms 8 · pad (298) | Yes |
```

### Event Type: Channel-Update (Appendix A, line 679)

```
| **channel-update** | 0x1F | channel_id 16 · new_channel_name 32 · new_disappearing_time_ms 8 · global_count 4 · pad (294) | Yes |
```

### Forward Secrecy Integration (lines 251-276)

> "When events are deleted or expire, we mark their associated keys and prekeys as 'must purge'."

> "Periodically, we create `rekey` events for all *not deleted* events associated with 'must-purge' keys and prekeys, encrypted deterministically to the 'clean' key whose `ttl` is minimally greater than the event `ttl`."

> "Periodically, we also purge the events that have corresponding, validated `rekey` events."

---

## Current Implementation Status

### What's Already Implemented ✅
- Message TTL storage (hardcoded to 1 week)
- TTL-based purge_expired mechanism
- Forward secrecy key purging with message_rekey events
- Message deletion with cascade deletion
- Event dependency tracking
- Per-peer scoped deletion tracking

### What's Missing ❌
- Per-channel `disappearing_time_ms` field in channel schema
- Channel-update event type and handling
- Dynamic message TTL based on channel's disappearing_time <!--skip this for now-->
- Seen event TTL validation against channel disappearing_time <!--skip this for now-->
- Update-based message TTL changes (when channel disappearing_time changes) 

---

## Implementation Tasks

### Phase 1: Schema & Data Model

#### Task 1.1: Update Channel Schema
**File**: `events/content/channel.sql`

- Add `disappearing_time_ms` column to `channels` table
  - Type: INTEGER, default 0 (permanent)
  - Semantics: milliseconds; 0 = never disappears
  - NOT NULL constraint

**SQL Addition**:
```sql
ALTER TABLE channels ADD COLUMN disappearing_time_ms INTEGER NOT NULL DEFAULT 0;
```

#### Task 1.2: Create Channel-Update Event Type
**Files**:
- `events/content/channel_update.py` (new)
- `events/content/channel_update.sql` (new)

**Schema**:
```sql
CREATE TABLE IF NOT EXISTS channel_updates (
    update_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    updated_by TEXT NOT NULL,  -- peer_shared_id
    global_count INTEGER NOT NULL,
    new_channel_name TEXT,  -- NULL if name not changed
    new_disappearing_time_ms INTEGER,  -- NULL if disappearing_time not changed
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (update_id, recorded_by)
);
```

**Event Structure**:
```python
class ChannelUpdate:
    """
    Represents a channel update event (type 0x1F).

    Fields:
    - channel_id: ID of channel being updated (16 bytes)
    - new_channel_name: New channel name or empty (32 bytes, zero-padded)
    - new_disappearing_time_ms: New disappearing time in ms (8 bytes)
    - global_count: Update ordering counter (4 bytes)
    """
```

---

### Phase 2: Event Creation & Projection

#### Task 2.1: Update Channel.create()
**File**: `events/content/channel.py`

**Changes**:
- Add `disappearing_time_ms` parameter (optional, default 0)
- Store in channel_creation_event plaintext
- Validate: must be non-negative integer
- Document: "0 = permanent, >0 = milliseconds until expiration"

**Pseudo-code**:
```python
def create(
    group_id: str,
    channel_name: str,
    disappearing_time_ms: int = 0,  # NEW PARAMETER
    created_by: str = None,
) -> str:
    # ... existing validation ...

    # NEW: Validate disappearing_time_ms
    if disappearing_time_ms < 0:
        raise ValueError("disappearing_time_ms must be non-negative")

    # Create channel event with disappearing_time_ms
    channel_event = {
        'type': 0x01,
        'group_id': group_id,
        'channel_name': channel_name,
        'disappearing_time_ms': disappearing_time_ms,  # NEW
        'created_by': created_by,
        'created_at': get_time_ms(),
    }

    # ... encrypt, sign, store ...
```

#### Task 2.2: Implement Channel.update()
**File**: `events/content/channel.py` (or new `channel_update.py`)

**Function Signature**:
```python
def create(
    channel_id: str,
    new_channel_name: str = None,
    new_disappearing_time_ms: int = None,
    updated_by: str = None,
) -> str:
    """
    Create a channel-update event.

    Args:
        channel_id: Channel to update
        new_channel_name: New name (or None to keep existing)
        new_disappearing_time_ms: New disappearing time (or None to keep existing)
        updated_by: peer_shared_id of updater (must be admin)

    Returns:
        update_id (event_id)

    Raises:
        ValueError: If not authorized or invalid parameters
    """
```

**Authorization**:
- Updater must be admin in the channel's group
- Check `invite.is_admin(updated_by, channel.group_id)`

**Validation**:
- At least one field must be provided (name or disappearing_time_ms)
- new_disappearing_time_ms must be non-negative if provided
- new_channel_name must be non-empty if provided
- Calculate and use global_count (max existing + 1)

**Create Event**:
- Type: 0x1F (channel-update)
- Fields: channel_id, new_channel_name, new_disappearing_time_ms, global_count
- Encrypted with channel's group key
- Signed by updater

#### Task 2.3: Implement Channel.project() Updates
**File**: `events/content/channel.py`

**New Projection Logic**:
```python
def project(update_id: str, recorded_by: str):
    """
    Project a channel-update event.

    1. Unwrap/decrypt the update event
    2. Validate authorization (creator is admin)
    3. Find the target channel
    4. If new_channel_name provided:
       - Update channel.name
       - Delete old channel_updates with higher global_count
    5. If new_disappearing_time_ms provided:
       - Update channel.disappearing_time_ms
       - Mark all existing messages in this channel for TTL recalculation
       - (See Phase 3, Task 3.1 for cascade behavior)
    6. Insert into channel_updates table
    7. Mark as valid_event
    """
```

**Key Detail**: When disappearing_time_ms changes, existing messages' TTL values should be updated on next projection cycle (or marked for update).

#### Task 2.4: Implement Seen Event TTL Validation
**File**: `events/content/seen.py`

**Current Validation** (line 416 of spec):
```
Validation: Signer in channel; message exists with created_at_ms <= viewed_at_ms.
TTL matches channel's disappearing time.
```

**New Logic**:
```python
def validate(seen_event, recorded_by):
    """
    Enhanced seen event validation.

    Existing checks:
    - Creator is member of channel
    - Message exists
    - viewed_at_ms >= message.created_at_ms

    NEW:
    - Calculate expected_ttl = message.created_at_ms + channel.disappearing_time_ms
    - If seen event's ttl_ms != expected_ttl: validation error
      (Different peers must see same TTL for convergence)
    """
```

---

### Phase 3: Message TTL Calculation & Updates

#### Task 3.1: Update Message.create() TTL Calculation
**File**: `events/content/message.py`

**Current Logic** (hardcoded):
```python
ttl_ms = created_at + DEFAULT_MESSAGE_TTL_MS  # 1 week
```

**New Logic**:
```python
def create(text: str, channel_id: str, created_by: str = None) -> str:
    # ... existing validation ...

    # NEW: Get channel's disappearing_time
    channel = safedb.query_one(
        "SELECT disappearing_time_ms FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (channel_id, recorded_by)
    )

    if not channel:
        raise ValueError(f"Channel {channel_id} not found")

    created_at = get_time_ms()
    disappearing_time_ms = channel['disappearing_time_ms']

    # Calculate TTL based on channel's disappearing_time
    if disappearing_time_ms == 0:
        ttl_ms = 0  # Never expires
    else:
        ttl_ms = created_at + disappearing_time_ms

    message_event = {
        'type': 0x00,
        'channel_id': channel_id,
        'text': text,
        'created_by': created_by,
        'created_at': created_at,
        'ttl_ms': ttl_ms,  # Dynamically calculated
    }

    # ... encrypt, sign, store ...
```

#### Task 3.2: Handle Channel Disappearing Time Changes
**File**: `events/content/message.py` and/or dedicated job

**Problem**: When a channel's disappearing_time_ms is updated, existing messages' TTL values become wrong.

**Solution Options**:

**Option A: Lazy Recalculation (Recommended for convergence)**
- When projecting a message, check if channel's disappearing_time_ms has changed since message creation
- Recalculate TTL on-the-fly during projection
- Don't store new TTL; keep original message unchanged

**Option B: Event-based Update (More Complex)**
- Create `message_ttl_update` events when channel disappearing_time changes
- Similar to message_rekey pattern
- Requires coordination to ensure convergence

**Recommendation**: Implement **Option A** for simplicity and convergence.

```python
def project(message_id: str, recorded_by: str):
    """
    Enhanced message projection with dynamic TTL.
    """
    # ... existing logic ...

    # NEW: Recalculate TTL based on current channel settings
    channel = safedb.query_one(
        "SELECT disappearing_time_ms FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (channel.channel_id, recorded_by)
    )

    if channel:
        if channel['disappearing_time_ms'] == 0:
            effective_ttl = 0
        else:
            effective_ttl = message.created_at + channel['disappearing_time_ms']
    else:
        effective_ttl = message.ttl_ms  # Fallback to stored value

    # Use effective_ttl for expiration checks, but don't modify stored value

    # ... rest of projection ...
```

---

### Phase 4: Message Expiration & Key Purging

#### Task 4.1: Update purge_expired() for Channel TTL
**File**: `purge_expired.py`

**Current Logic**:
- Delete all events with `ttl_ms > 0 AND ttl_ms <= cutoff_ms`

**New Requirements**:
- Same logic applies (no changes needed)
- Messages with dynamic TTL will be purged when their calculated ttl_ms expires

**Note**: The `ttl_ms` field stored in the message still works as-is. The dynamic recalculation in Task 3.2 is only for active validation; expiration uses stored values.

**Alternative Approach** (if dynamic recalculation not taken):
- Update message records when channel disappearing_time changes
- This would require update events or batch recalculation

#### Task 4.2: Forward Secrecy Integration
**File**: `events/content/message_deletion.py` and `events/content/message_rekey.py`

**Current Behavior** (already implemented):
- When message is deleted, its key is marked in `keys_to_purge`
- `run_message_purge_cycle()` finds all messages using marked keys
- Re-encrypts with new "clean" key using deterministic nonce
- Deletes old key after all messages rekeyed

**Required for Disappearing Messages**:
- Same logic applies for expired (disappearing) messages
- When a message expires (ttl_ms <= current_time), mark its key for purging
- `run_message_purge_cycle()` will handle the rekeying

**Key Addition**:
```python
# In purge_expired.py or dedicated job
def mark_expired_message_keys_for_purge(cutoff_ms: int):
    """
    After purging expired messages, mark their keys for purging.

    1. Find all messages with ttl_ms > 0 AND ttl_ms <= cutoff_ms (expired)
    2. For each expired message, get its encryption key_id
    3. Insert into keys_to_purge if not already marked
    """
```

---

### Phase 5: Testing & Validation

#### Task 5.1: Unit Tests - Channel Creation with Disappearing Time
**File**: `tests/scenario_tests/test_channels.py`

**Test Cases**:
- `test_channel_creation_permanent()` - disappearing_time_ms = 0
- `test_channel_creation_with_disappearing_time()` - disappearing_time_ms = 24hrs
- `test_channel_creation_negative_ttl_fails()` - validation error
- `test_multiple_channels_different_ttls()` - independent TTLs

#### Task 5.2: Unit Tests - Channel Updates
**File**: `tests/scenario_tests/test_channels.py`

**Test Cases**:
- `test_channel_update_name_only()` - Update name, preserve TTL
- `test_channel_update_disappearing_time_only()` - Update TTL, preserve name
- `test_channel_update_both_fields()` - Update both
- `test_channel_update_unauthorized()` - Non-admin rejection
- `test_channel_update_idempotent()` - Same update twice (global_count handling)

#### Task 5.3: Integration Tests - Message TTL & Expiration
**File**: `tests/scenario_tests/test_disappearing_messages.py` (new)

**Test Cases**:
- `test_message_ttl_set_from_channel()` - Message TTL = channel's disappearing_time
- `test_message_expiration_single_peer()` - Message expires locally
- `test_message_expiration_multi_peer()` - Message expires on all peers
- `test_channel_ttl_change_applies_to_new_messages()` - New messages use updated TTL
- `test_channel_ttl_change_affects_existing_messages()` - Existing messages expire with new TTL
- `test_permanent_channel_messages_never_expire()` - disappearing_time_ms = 0

#### Task 5.4: Integration Tests - Forward Secrecy
**File**: `tests/scenario_tests/test_disappearing_messages.py`

**Test Cases**:
- `test_expired_message_key_marked_for_purge()` - Key added to keys_to_purge
- `test_message_rekey_on_expiration()` - Rekeying called after expiration
- `test_key_purged_after_rekey()` - Old key deleted from group_keys
- `test_multi_peer_forward_secrecy()` - All peers converge on rekeyed state

#### Task 5.5: Convergence Tests - Seen Events
**File**: `tests/scenario_tests/test_disappearing_messages.py`

**Test Cases**:
- `test_seen_event_ttl_matches_channel()` - Validation passes with correct TTL
- `test_seen_event_ttl_mismatch_validation()` - Validation fails with wrong TTL
- `test_seen_event_after_channel_ttl_change()` - Seen events use new TTL

#### Task 5.6: API Tests
**File**: `tests/integration/test_api.py` (updates)

**Test Cases**:
- `POST /networks/{network_id}/channels` with disappearing_time_ms
- `PATCH /networks/{network_id}/channels/{channel_id}` with new disappearing_time
- Verify message TTL reflects channel settings in API responses

---

## Architecture Decisions

### TTL Calculation Strategy

**Decision**: Dynamic recalculation (Option A) during message projection

**Rationale**:
- Simpler than event-based updates
- Converges automatically (all peers recalculate same way)
- No new event types needed
- Backward compatible (old messages still have stored ttl_ms as fallback)

### Message Storage

**Decision**: Keep stored `ttl_ms` unchanged; calculate effective TTL during projection

**Rationale**:
- No database migrations needed for existing messages
- Existing forward secrecy logic uses stored `ttl_ms`
- Effective TTL only used for validation, not persistence

### Key Purging

**Decision**: Reuse existing forward secrecy purging pipeline

**Rationale**:
- Avoids duplicating key lifecycle management
- Consistent with message_deletion behavior
- Proven design in codebase

### Authorization

**Decision**: Only admins can create/update channels with disappearing times

**Rationale**:
- Matches spec: "Only members of the admin group can create channels"
- Prevents users from unilaterally expiring messages
- Consistent with message deletion authorization

---

## Implementation Timeline

| Phase | Tasks | Est. Effort |
|-------|-------|------------|
| 1: Schema & Data Model | 1.1, 1.2 | 2 days |
| 2: Event Creation & Projection | 2.1, 2.2, 2.3, 2.4 | 4 days |
| 3: Message TTL | 3.1, 3.2 | 2 days |
| 4: Expiration & Key Purging | 4.1, 4.2 | 2 days |
| 5: Testing & Validation | 5.1-5.6 | 5 days |
| **TOTAL** | | **15 days** |

---

## Dependencies & Blockers

### Hard Dependencies
- ✅ Message deletion (message_deletion event type)
- ✅ Forward secrecy (key purging, message_rekey)
- ✅ Channel creation (channel event type)
- ✅ Admin authorization (invite.is_admin checks)

### Soft Dependencies
- Message updates (for future feature: editing messages before expiration)
- Seen events (for read receipts with correct TTL)

### Known Risks

1. **Convergence on Channel TTL Changes**
   - Risk: Different peers apply TTL change at different times
   - Mitigation: Lazy recalculation ensures eventual consistency
   - Test: test_channel_ttl_change_multi_peer_sync

2. **Key Expiration vs Message Deletion**
   - Risk: Expired message keys might be purged before deletion processed
   - Mitigation: Deletion marks key for purging, expires messages do too (same queue)
   - Test: test_concurrent_deletion_and_expiration

3. **Seen Event TTL Validation**
   - Risk: Seen events might have wrong TTL if channel changed
   - Mitigation: Validate against current channel state
   - Test: test_seen_event_after_channel_ttl_change

---

## Future Enhancements

1. **Bulk Channel TTL Updates**
   - Update TTL for all messages in a channel at once (not lazy)
   - Useful for compliance (e.g., "all messages expire after 30 days")

2. **Per-Message TTL Overrides**
   - Allow senders to set custom TTL for specific messages
   - Require: new update event type for message TTL

3. **TTL Alerts & Warnings**
   - Notify users when messages are about to expire
   - Show expiration time in UI

4. **TTL Statistics**
   - Track: how many messages expire per channel per day
   - Useful for compliance and audit

5. **Selective Retention**
   - Allow pinned messages to never expire
   - Allow archived messages to have custom TTL

---

## References

- `docs/ideal_protocol_design.md` - Channels section (lines 384-418)
- `events/content/channel.py` - Current channel implementation
- `events/content/message_deletion.py` - Forward secrecy pattern
- `purge_expired.py` - TTL-based expiration
- `tests/scenario_tests/test_message_deletion.py` - Deletion patterns for testing

---

## Sign-Off

**Document Created**: 2025-11-11
**Status**: Ready for implementation planning review
**Next Step**: Begin Phase 1 (Schema & Data Model)
