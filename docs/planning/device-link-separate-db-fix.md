# Device Linking Bug: v2 Resolver Blocks Before Fallback

## Problem Summary

Device linking fails when using separate databases (real networking tests) because the v2 resolver blocks `peer_shared` events before the fallback code can run.

**Works in**: Scenario tests (single shared database)
**Fails in**: Real networking tests (separate databases per client)

## Root Cause Analysis

### The Expected Flow

When laptop links to phone via peer invite:

1. **Phone creates** `invite(mode=peer)` → stored in phone's `invites` table
2. **Invite link** contains `invite_id`, `invite_private_key`, `user_id`
3. **Laptop accepts** → creates `invite_accepted` with private key
4. **Laptop creates** `peer_shared` event signed by `invite_id`
5. **`peer_shared.project()`** should verify signature and project

### The Blocking Issue

The code already has fallback logic in `peer_shared.project()` (lines 228-242) that derives `invite_pubkey` from `invite_accepteds.invite_private_key`. This was added in the bootstrap-sync-fix merge.

**However**: The v2 projection path blocks the event BEFORE this fallback can run:

1. `recorded.project()` calls `v2_resolver.resolve_event()` (line 393)
2. Resolver checks `peer_shared.EVENT_SPEC.optional.invite` dependency
3. `required_if_present: True` means if invite_id exists, invite must be in valid_events
4. Invite is NOT in valid_events (it's on phone's database)
5. Resolver returns `status='block'` (line 106-107 in resolver.py)
6. Event goes to blocked queue (line 432)
7. `project_pure()` is NEVER called - fallback is unreachable

### Debug Evidence

```
Blocking peer_shared event RtskfHT9MnrwLX/QpIuU... recorded_by=9Zs8Y52MD1ROnSy0CclJ...
  requester_peer_shared=N/A... missing deps: ['fycVJ93uccadUbo/v+NU']
```

The `fycVJ93uccadUbo/v+NU` is the peer invite created on Alice's phone. The laptop's peer_shared blocks waiting for it, but it will never be delivered (peer invites are local-only for security).

## Why Scenario Tests Pass

Scenario tests use a single shared database. The invite IS in the same database's `invites` table and `valid_events`, so the v2 resolver doesn't block.

## The Fix: Resolver Fallback

The fix must happen in the v2 resolver, not in `peer_shared.project()`. Two options:

### Option A: Add `fallback_table` to dependency specs (Recommended)

Add a generic mechanism to the resolver that checks a fallback table when the primary lookup fails:

**File**: `events/identity/peer_shared.py`

```python
EVENT_SPEC = {
    'optional': {
        'invite': {
            'source': 'table',
            'table': 'invites',
            'key': 'invite_id',
            'key_from': 'invite_id',
            'fields': ['invite_id', 'invite_pubkey', 'user_id'],
            'required_if_present': True,
            # NEW: Check this table if primary fails (for device linking bootstrap)
            'fallback_table': 'invite_accepteds',
            'fallback_condition': 'owner_peer_id == recorded_by',  # Only for own events
        },
    },
}
```

**File**: `core/projection_v2/resolver.py` (around line 105)

Add fallback logic when primary table lookup fails:

```python
if not _is_event_valid(dep_id, recorded_by, safedb):
    # Check for fallback table (device linking bootstrap case)
    fallback_table = dep_spec.get('fallback_table')
    fallback_condition = dep_spec.get('fallback_condition')

    if fallback_table and _should_use_fallback(fallback_condition, event_data, recorded_by):
        fallback_row = _fetch_fallback_row(fallback_table, dep_id, recorded_by, safedb)
        if fallback_row:
            # Derive data from fallback (e.g., pubkey from private key)
            return "ok", fallback_row, [], None

    if required or dep_spec.get("required_if_present"):
        return "block", None, [dep_id], None
```

### Option B: Special-case peer_shared in resolver

Add explicit handling for peer_shared events when `owner_peer_id == recorded_by`:

**File**: `core/projection_v2/resolver.py`

```python
# In _resolve_table_dep(), after line 107:
if not _is_event_valid(dep_id, recorded_by, safedb):
    # Special case: peer_shared with invite on another device
    # For device linking, the invite exists only on the inviting device.
    # If this is our own peer_shared and we have invite_accepted, don't block.
    if event_type == 'peer_shared' and event_data.get('peer_id') == recorded_by:
        ia_row = safedb.query_one(
            "SELECT invite_private_key, user_id FROM invite_accepteds WHERE invite_id = ? AND recorded_by = ?",
            (dep_id, recorded_by)
        )
        if ia_row and ia_row['invite_private_key']:
            from nacl.signing import SigningKey
            priv_key = ia_row['invite_private_key']
            signing_key = SigningKey(priv_key)
            invite_pubkey = bytes(signing_key.verify_key)
            return "ok", {
                'invite_pubkey': crypto.b64encode(invite_pubkey),
                'user_id': ia_row['user_id'],
            }, [], None

    if required or dep_spec.get("required_if_present"):
        return "block", None, [dep_id], None
```

## Why the Existing Fallback Doesn't Work

The merge from master brought in fallback code in `peer_shared.project()` (legacy projector):

```python
# Lines 228-242 in peer_shared.py
owner_peer_id = event_data['peer_id']
if not invite_pubkey_bytes and owner_peer_id == recorded_by:
    ia_row = safedb.query_one(
        "SELECT invite_private_key, user_id FROM invite_accepteds WHERE invite_id = ? AND recorded_by = ?",
        (invite_id, recorded_by)
    )
    if ia_row and ia_row['invite_private_key']:
        from nacl.signing import SigningKey
        priv_key = ia_row['invite_private_key']
        signing_key = SigningKey(priv_key)
        invite_pubkey_bytes = bytes(signing_key.verify_key)
        user_id = ia_row['user_id']
```

This code is correct but **never reached** because:
1. peer_shared has `EVENT_SPEC` and `project_pure()` defined
2. This triggers v2 projection path in recorded.project() (line 389)
3. v2 resolver blocks before `project_pure()` runs
4. The legacy `project()` function (with fallback) is never called

## Files to Modify

| File | Change |
|------|--------|
| `core/projection_v2/resolver.py` | Add fallback logic for invite_accepteds |
| Optionally: `events/identity/peer_shared.py` | Add `fallback_table` to EVENT_SPEC |

## Testing

After fix:
```bash
# Run device link tests
PYTHONPATH=. pytest tests/networking/test_device_link.py -v --tb=short

# Run all networking tests
PYTHONPATH=. pytest tests/networking/ -v

# Run scenario tests to ensure no regression
PYTHONPATH=. pytest tests/scenario_tests/ -v --tb=short
```

## Design Justification

1. **Resolver-level fix** is necessary because v2 projection blocks before projector runs
2. **Fallback for crypto verification** is explicitly allowed per RULES.md Gotcha 6
3. **`invite_accepteds` table** already stores `invite_private_key` for this purpose
4. **Option A (generic fallback)** is cleaner and reusable for future similar cases
5. **Option B (special-case)** is simpler but less maintainable

## Related Documentation

- `docs/planning/bootstrap-sync-hacks.md` - Bootstrap fallback patterns
- `docs/quiet-protocol-specification.md` - Trust anchor mechanism (lines 344-385)
- `docs/archive/isomorphic-peer-linking-plan.md` - Device linking design
- `RULES.md` - Gotcha 6 (Store Blob Fallbacks exception)

## Status

- [x] Issue documented
- [x] Root cause identified (v2 resolver blocking)
- [x] Fix options designed
- [ ] Fix implemented
- [ ] Tests passing
