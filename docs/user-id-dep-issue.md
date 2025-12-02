# Issue: user_id in peers_shared is a string, not a real dependency

## Problem

`peers_shared.user_id` is populated from the invite event's `user_id` field, but there's no guarantee the user event is actually valid (in `valid_events`) when peer_shared projects.

This means:
- `peer_shared` can be marked valid before its `user_id` is valid
- Events that derive `author_id` from `signed_by` → `peers_shared.user_id` could reference non-existent users
- The dependency system is bypassed for this relationship

## How it happens

### peer_shared.project() (lines 102-114)
```python
invite_row = safedb.query_one(
    "SELECT invite_pubkey, user_id FROM invites WHERE invite_id = ? ..."
)
user_id = invite_row['user_id']  # Just a string from invites table!

# Stores user_id without checking if user is in valid_events
safedb.execute(
    """INSERT OR IGNORE INTO peers_shared
       (..., user_id, ...)
       VALUES (..., ?, ...)""",
    (..., user_id, ...)  # No validity check!
)
```

### invite.project() (line 576-589)
```python
user_id = event_data.get('user_id')  # From invite event (mode='peer')
# Stores in invites table - no check that user event exists/is valid
```

## Impact

1. **Message author derivation would be unsafe** - Can't remove `author_id` from message events because deriving from `signed_by` → `peers_shared.user_id` doesn't guarantee user validity

2. **Potential data integrity issues** - Other code paths that trust `peers_shared.user_id` may operate on invalid user references

## Investigation Needed

Are there other ID fields that are "just strings" without real dependency checks?

Candidates to audit:
- `invites.user_id` - stored from event field, no validity check?
- `invites.inviter_id` - same issue?
- `group_members.user_id` - is this checked?
- `admins.user_id` - is this checked?

## Proposed Fix

### Option A: Add validity check in peer_shared.project()
```python
# Before storing user_id, verify it's valid
if user_id:
    valid_check = safedb.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (user_id, recorded_by)
    )
    if not valid_check:
        log.warning(f"peer_shared.project() user_id not valid yet")
        return None  # Block until user is valid
```

### Option B: Add user_id to peer_shared event and check_deps()
Make `user_id` an explicit field in peer_shared events so the standard dependency checker catches it.

### Option C: Foreign key constraint approach
Ensure `peers_shared.user_id` references `users.user_id` with proper ordering.

## Files to Modify

1. `events/identity/peer_shared.py` - Add validity check before storing user_id
2. `events/identity/invite.py` - Audit user_id handling
3. `events/network/recorded.py` - Potentially add peer_shared-specific dep handling

## Testing

- Test that peer_shared blocks if user not yet valid
- Test that message with derived author_id works correctly
- Test out-of-order event arrival scenarios
