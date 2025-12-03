# Deterministic Local Keys

## Goal

Make `group_key` and `group_prekey` event IDs deterministic from key material only, so the same keys produce the same event IDs across peers.

## Why

This enables natural cascade from `invite_accepted`:
- When inviter creates a `group_prekey`, it gets `prekey_id = hash(key_material)`
- Inviter seals `group_key_shared` to this `prekey_id`
- When joiner's `invite_accepted.project()` recreates the prekey from invite link data, it produces the **same** `prekey_id`
- Events blocked on that `prekey_id` naturally unblock

Without determinism, inviter and joiner would get different IDs for the same key, breaking the cascade.

## Spec Reference

See `docs/quiet-protocol-specification.md`:
- Section: "Deterministic Key Event IDs"
- Types table entries for `group_key` and `group_prekey`

## Current State

**group_prekey.py** (lines 77-85):
```python
event_data = {
    'type': 'group_prekey',
    'public_key': crypto.b64encode(prekey_public),
    'private_key': crypto.b64encode(prekey_private),
    'signed_by': peer_id,      # REMOVE
    'created_at': t_ms         # REMOVE
}
```

**group_key.py** - similar pattern, needs same fix.

## Tasks

### 1. Update `group_prekey.create()`
- Remove `signed_by` and `created_at` from event_data
- Event should only contain: `type`, `public_key`, `private_key`
- The `prekey_id` becomes deterministic: `hash(canonical({type, public_key, private_key}))`

### 2. Update `group_prekey.project()`
- Currently expects `signed_by` field - update to work without it
- May need to pass `owner_peer_id` separately or derive from context

### 3. Update `group_key.create()`
- Remove `signed_by` and `created_at` from event_data
- Event should only contain: `type`, `key`
- The `key_id` becomes deterministic: `hash(canonical({type, key}))`

### 4. Update `group_key.project()`
- Same as group_prekey - update to work without `signed_by`

### 5. Add `group_prekey.create_from_existing()`
- New function that takes existing key material and creates deterministic event
- Used by `invite_accepted.project()` to recreate the same prekey
- Returns the same `prekey_id` as the original

### 6. Update tests
- Verify determinism: same keys → same IDs
- Verify cascade: prekey created from invite link unblocks waiting events

## Validation

```python
# Same key material should produce same ID
key1 = b'...'
id1 = group_key.create_from_existing(key1, peer_id, t_ms, db)
id2 = group_key.create_from_existing(key1, peer_id, t_ms + 1000, db)  # Different time
assert id1 == id2  # Must be equal!
```

## Files to Modify

- `events/group/group_prekey.py`
- `events/group/group_key.py`
- `tests/` - relevant test files
