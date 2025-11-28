# Pure Functional Projector Conversion Plan

## Goal
Convert all projectors to pure functional style for:
- **Simplicity**: Clear separation of I/O from logic
- **Readability**: SPEC documents dependencies, project() is pure
- **Testability**: Pure tests without database setup

## Already Converted (6)
| Projector | Tests | Notes |
|-----------|-------|-------|
| message | 5 | TTL, deletion handling |
| channel | 4 | admin_grant authorization |
| group_member | 6 | admin_grant, legacy creator |
| user | 6 | invite validation, auto group_member |
| admin | 5 | polymorphic signing (network/peer) |
| network | 4 | self-signed root of trust |

**Current: 31 pure tests passing**

## To Convert - Priority 1 (Core Identity/Group)

These are the foundation events that other events depend on.

| Event Type | File | Complexity | Notes |
|------------|------|------------|-------|
| group | events/group/group.py | Low | Creates groups, signed by network or peer |
| peer_shared | events/identity/peer_shared.py | Low | Shared peer identity |
| invite | events/identity/invite.py | Medium | Bootstrap vs ongoing modes |
| invite_accepted | events/identity/invite_accepted.py | Medium | Links invite to user |

## To Convert - Priority 2 (Group Keys)

Encryption key distribution - critical for message security.

| Event Type | File | Complexity | Notes |
|------------|------|------------|-------|
| group_key | events/group/group_key.py | Low | Group encryption key creation |
| group_key_shared | events/group/group_key_shared.py | High | Key sharing, complex deps |
| group_prekey | events/group/group_prekey.py | Low | Prekeys for forward secrecy |
| group_prekey_shared | events/group/group_prekey_shared.py | Medium | Shared prekeys |

## To Convert - Priority 3 (Content Operations)

Operations on content (updates, deletions, rekeying).

| Event Type | File | Complexity | Notes |
|------------|------|------------|-------|
| channel_update | events/content/channel_update.py | Low | Updates channel settings |
| message_deletion | events/content/message_deletion.py | Medium | Authorization checks |
| message_rekey | events/content/message_rekey.py | Medium | Forward secrecy rekeying |

## To Convert - Priority 4 (Advanced Identity)

Less frequently used identity operations.

| Event Type | File | Complexity | Notes |
|------------|------|------------|-------|
| invite_proof | events/identity/invite_proof.py | Low | Proves invite acceptance |
| bootstrap_complete | events/identity/bootstrap_complete.py | Low | Marker event |
| link_invite_accepted | events/identity/link_invite_accepted.py | Medium | Device linking |

## Skip - Device-Local (Not Shareable)

These operate on device-local state, not shared events. Pure functional pattern doesn't apply.

| Event Type | Reason |
|------------|--------|
| peer | Device-local identity |
| transit_key | Device-local encryption |
| transit_prekey | Device-local prekeys |
| transit_prekey_shared | Transit layer |
| sync, sync_connect, sync_file | Protocol handling |
| intro, address | Network layer |
| recorded | Orchestrator, not projector |

## Skip - Complex Side Effects

These have significant side effects beyond INSERT OR IGNORE.

| Event Type | Reason |
|------------|--------|
| peer_removed | Triggers key rotation, complex cascades |
| user_removed | Triggers removal logic across tables |
| file_slice | File assembly state machine |
| message_attachment | File consolidation logic |
| network_created, network_joined | Legacy, may be deprecated |

## Conversion Pattern

For each projector:

### 1. Create `projectors/{name}.py`
```python
"""Event type projector.

SPEC - declares dependencies, signer_type, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - test builders
"""

from projectors import ProjectorResult

SPEC = {
    "encrypted": True/False,
    "signer_type": "peer_shared|network|invite|admin|self",
    "dependencies": ["dep_name:dep_type", ...],
    "tables": ["table_name", ...],
}

def project(input_dict: dict) -> ProjectorResult:
    """Pure projection: dict -> result. No database access."""
    ...

# Test builders
def make_event_data(...) -> dict: ...
def make_input(...) -> dict: ...
```

### 2. Update `projectors/__init__.py`
- Import and register in `_load_projectors()`
- Add any new dependency types to `_resolve_dependency()`
- Add any new signer types to `_verify_signature()`

### 3. Update `events/{category}/{name}.py`
```python
def project(...):
    from projectors import resolve, apply_result
    from projectors import {name} as {name}_projector

    input_dict = resolve("{name}", event_id, recorded_by, recorded_at, db)
    if not input_dict:
        return None

    result = {name}_projector.project(input_dict)

    if result.blocked or not result.valid:
        return None

    apply_result(result, recorded_by, recorded_at, db)

    # Any side effects that can't be INSERT OR IGNORE
    ...

    return event_id
```

### 4. Add tests to `projectors/tests.py`
- Basic valid case
- Missing required dependencies (blocked)
- Invalid signature
- Authorization failures
- Edge cases specific to event type

## Test Coverage Goals

| Projector | Target Tests | Coverage Areas |
|-----------|--------------|----------------|
| group | 4 | basic, network-signed, peer-signed, missing deps |
| peer_shared | 3 | basic, signature, missing peer |
| invite | 5 | bootstrap, ongoing, wrong signer, missing deps |
| invite_accepted | 4 | basic, signature, missing invite |
| group_key | 3 | basic, signature, wrong signer |
| group_key_shared | 5 | basic, signature, missing key, missing recipient |
| group_prekey | 3 | basic, signature, wrong signer |
| group_prekey_shared | 4 | basic, signature, missing prekey |
| channel_update | 4 | basic, admin auth, wrong admin |
| message_deletion | 5 | self-delete, admin-delete, unauthorized |
| message_rekey | 4 | basic, signature, old key exists |
| invite_proof | 3 | basic, signature, missing invite |
| bootstrap_complete | 2 | basic, signature |
| link_invite_accepted | 4 | basic, signature, missing link_invite |

**Target: ~55 additional tests → ~85 total pure tests**

## Execution Order

1. **Week 1**: Priority 1 (group, peer_shared, invite, invite_accepted)
2. **Week 2**: Priority 2 (group_key, group_key_shared, group_prekey, group_prekey_shared)
3. **Week 3**: Priority 3 + 4 (remaining)

## Success Criteria

- [ ] All priority 1-4 projectors converted
- [ ] 80+ pure tests passing
- [ ] All existing scenario tests still pass
- [ ] Each projector has test coverage for:
  - Valid case
  - Blocked on missing deps
  - Invalid signature/authorization
