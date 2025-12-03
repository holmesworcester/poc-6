# invite_accepted as Trust Anchor

## Goal

Make `invite_accepted` the trust anchor for joining by:
1. Storing complete raw invite link data in the event
2. Moving all interpretation logic to `invite_accepted.project()`
3. Having projection mark `network_id` as valid, triggering the cascade

## Why

**Event-sourcing principle**: Events are immutable facts containing raw input; projectors interpret those facts into state.

Currently, joining logic is scattered:
- `user.join()` parses invite link, extracts fields
- `user.join()` manually projects inviter's peer_shared
- `invite_accepted` stores only partial data
- `invite_accepted.project()` does manual table inserts and `notify_event_valid()` calls

After this change:
- `invite_accepted` event contains the complete invite link (raw)
- `invite_accepted.project()` is the single source of truth for "what accepting means"
- Natural cascade via standard blocking/unblocking mechanism
- Complete reprojection works without the original invite link

## Spec Reference

See `docs/quiet-protocol-specification.md`:
- Section: "Invite Acceptance and Trust Anchoring"
- Subsections: "Event Structure", "Projection and Trust Cascade", "Why This Architecture"

## Current State

**invite_accepted event** (current):
```python
{
    'type': 'invite_accepted',
    'invite_id': invite_id,
    'invite_prekey_id': invite_prekey_id,
    'invite_private_key': crypto.b64encode(invite_private_key),
    'signed_by': peer_id,
    'created_at': t_ms
}
```

**invite_accepted.project()** (current):
- Manual insert into `group_prekeys` table
- Manual `notify_event_valid()` calls
- Doesn't mark network_id as valid

## Tasks

### 1. Update `invite_accepted.create()` to store raw invite link data

New structure:
```python
{
    'type': 'invite_accepted',
    'invite_link_data': {
        'invite_blob': ...,              # Complete signed invite event
        'invite_private_key': ...,       # For prekey and signing
        'invite_prekey_id': ...,         # Crypto hint
        'inviter_peer_shared_blob': ..., # For sync (optional)
        'inviter_transit_prekey': ...,   # For connection (optional)
        'network_id': ...,               # Which network
        # ... all other invite link fields
    },
    'signed_by': peer_id,
    'created_at': t_ms
}
```

### 2. Update `invite_accepted.project()` for trust cascade

```python
def project(invite_accepted_id, recorded_by, recorded_at, db):
    # 1. Parse raw invite link data
    invite_link_data = event_data['invite_link_data']

    # 2. Mark network_id as valid (TRUST ANCHOR)
    network_id = invite_link_data['network_id']
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (network_id, recorded_by)
    )

    # 3. Create deterministic group_prekey from key material
    # (Depends on deterministic-local-keys branch)
    prekey_id = group_prekey.create_from_existing(
        public_key=...,
        private_key=invite_link_data['invite_private_key'],
        peer_id=recorded_by,
        db=db
    )
    # This naturally triggers notify_event_valid(prekey_id) via normal projection

    # 4. Cascade unblock happens automatically via blocking/unblocking mechanism
    # Events waiting on network_id or prekey_id will now project
```

### 3. Simplify `user.join()` / `invite.accept()`

Remove scattered logic:
- Don't parse invite link in join function
- Don't manually project inviter's peer_shared
- Just create `invite_accepted` with raw data and let projection handle it

```python
def join(peer_id, invite_link, name, t_ms, db):
    # Parse just enough to validate format
    invite_link_data = parse_invite_link(invite_link)

    # Create invite_accepted with complete raw data
    invite_accepted.create(
        invite_link_data=invite_link_data,
        peer_id=peer_id,
        t_ms=t_ms,
        db=db
    )

    # Create user event (signed by invite)
    user.create(...)

    # Create peer_shared event
    peer_shared.join(...)

    # All projection happens naturally
```

### 4. Update schema if needed

May need to update `invite_accepteds` table to match new structure, or let projection populate it from the raw data.

### 5. Update tests

- Test that network_id becomes valid after invite_accepted.project()
- Test cascade: blocked events unblock when network_id is valid
- Test reprojection: drop tables, replay events, same state

## Dependencies

This branch depends on **deterministic-local-keys** for step 3 (creating prekey from existing key material with same ID).

Can be developed in parallel but final integration needs deterministic keys.

## Files to Modify

- `events/identity/invite_accepted.py`
- `events/identity/invite_accepted.sql` (if schema changes)
- `events/identity/user.py` (simplify join)
- `events/identity/invite.py` (simplify accept)
- `tests/` - joining and cascade tests
