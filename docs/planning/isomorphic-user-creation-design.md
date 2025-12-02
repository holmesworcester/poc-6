# Isomorphic User Creation Design

## Overview

User creation follows a single, isomorphic code path for both network creators (Alice) and invite joiners (Bob). The `user.create()` function has **zero conditional logic** to distinguish between these cases.

## Design Principles

1. **Single Code Path**: One `create()` function handles all user creation
2. **Invite-Agnostic**: `create()` doesn't know or care where the invite came from
3. **Build Missing Pieces Externally**: Differences are in invite construction, not user creation
4. **Content-Addressed Identity**: `user_id = id(user_event)`, not `invite_id`

## User Event Structure

```python
{
    'type': 'user',
    'invite_id': str,           # Reference to authorizing invite
    'signed_by': invite_id,     # Polymorphic signer (verified with invite_pubkey)
    'user_pubkey': str,         # User's OWN fresh keypair (base64)
    'name': str,                # Display name
    'created_at': int,          # Timestamp
    'network_id': str | None    # Optional, extracted from invite
}
```

**Critical**: User events do NOT contain `peer_id`. The user-to-peer relationship is established when `peer_shared` is projected (stored in `peers_shared` table).

## The `create()` Function

```python
def create(peer_id: str, name: str, t_ms: int, db: Any,
           invite_id: str,
           invite_private_key: bytes) -> tuple[str, bytes]:
    """Create a user event representing network membership.

    Returns: (user_id, user_private_key)
    """
```

The function:
1. Gets invite blob from store, extracts `network_id`
2. Generates fresh user keypair
3. Builds user event (structure above)
4. Signs with `invite_private_key`
5. Stores via `store.event()` (includes projection)
6. Returns `(user_id, user_private_key)`

## Flow Comparison

### Network Creator (Alice) - `new_network()`

```
peer.create()                           # Local peer
network.create()                        # Self-signed network (root of trust)
invite.create_bootstrap_user_invite()   # Invite signed by network
    group_id='', channel_id='', key_id=''  # Empty - content created later

user.create(                            # <-- SAME FUNCTION
    peer_id, name, t_ms, db,
    invite_id, invite_private_key
)

# Content created AFTER user exists:
peer_shared.join()
admin.create()
group.create()
channel.create()
```

### Invite Joiner (Bob) - `join()`

```
# Parse invite link (from existing network member)
invite_link → invite_id, invite_private_key, group_id, channel_id, ...
store.event(invite_blob)               # Store the invite
invite_accepted.create()               # Store secrets for reprojection

user.create(                           # <-- SAME FUNCTION
    peer_id, name, t_ms, db,
    invite_id, invite_private_key
)

peer_shared.join()
# Group/channel already exist - discovered via sync
```

## Why It Works

The key insight is that `create()` only needs:
- An `invite_id` that exists in the store
- The matching `invite_private_key` to sign the user event

It extracts `network_id` from the invite (for inclusion in user event), but doesn't use `group_id`, `channel_id`, or `key_id`. Those fields exist in the invite for the **caller** to use when setting up group membership and sync.

| Invite Type | group_id/channel_id/key_id | network_id |
|-------------|---------------------------|------------|
| Bootstrap   | Empty strings             | Present    |
| Regular     | Real IDs                  | Present    |

Both work with `create()` because the function only uses `network_id`.

## Key Separation

### Invite Keypair (from invite link)
- `invite_private_key`: Received in invite link
- `invite_pubkey`: Stored in `invite(mode=user)` event
- **Purpose**: Signs the `user` event (`signed_by=invite_id`)
- **Lifecycle**: Discarded after creating `user` event

### User Keypair (freshly generated)
- `user_private_key, user_pubkey = generate_keypair()` - fresh, NOT derived from invite
- `user_pubkey`: Stored IN the `user` event body
- **Purpose**: Signs first `invite(mode=peer)` (`signed_by=user_id`)
- **Lifecycle**: Discarded after first `peer_shared` is created

This separation ensures:
- Invite key proves "I have the invite link" (one-time use)
- User key proves "I am this specific user" (short-lived, first-peer only)
- After first peer links, all subsequent operations use `peer_shared` keys

## Implementation Notes

### Removed Code
- `join_bootstrap()` function deleted - was duplicate of `create()`
- Dead parameters removed from `create()` signature
- Dead `skip_dep_check` logic removed from `recorded.py`

### What Changed
- `create()` signature simplified: `(peer_id, name, t_ms, db, invite_id, invite_private_key)`
- Return value simplified: `(user_id, user_private_key)` instead of 4-tuple with None placeholders
- Both `new_network()` and `join()` now call the same `create()` function

## Related Documents

- `docs/quiet-protocol-specification.md` - Protocol-level specification
- `docs/planning/network-root-linking-design.md` - DAG and signing model
- `docs/archive/isomorphic-peer-linking-plan.md` - Original plan for this work
