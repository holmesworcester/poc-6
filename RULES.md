# Development Rules

Core principles and common gotchas for working on this codebase.

## Core Principles

### 1. Event-Sourced Architecture

All state comes from events. The flow is:

```
event.create() → store.event() → recorded.project() → type.project() → table rows
```

**DO:**
- Create events via `store.event()` which handles the full cascade
- Let `recorded.project()` handle dependency checking and dispatch
- Trust the projection system - if deps are valid, events project

**DON'T:**
- Insert directly into projection tables (bypasses event-sourcing)
- Force projection with explicit `project_ids()` calls (redundant)
- Add "defensive" code that masks the real dependency model

### 2. DAG-Based Dependency Resolution

Events declare dependencies via fields like `signed_by`, `invite_id`, `group_id`. The `check_deps()` function in `recorded.py` validates these before projection.

**DO:**
- Declare dependencies explicitly in event data
- Let `check_deps()` block events until deps are valid
- Create events in dependency order (network → invite → user → admin → ...)

**DON'T:**
- Use timestamp offsets (`t_ms + N`) to fake ordering
- Add `skip_*` flags to bypass validation
- Add fallback code that masks missing dependencies

### 3. Cryptographic Authorization Chain

Authorization flows through signatures, not runtime checks:

```
Network (self-signed, root of trust)
    ↓ signs
Admin grant (network signs first admin)
    ↓ signs
Invites, group members, etc.
```

**DO:**
- Sign events with the appropriate key (network, user, or peer)
- Verify signatures in projectors using the declared `signed_by`
- Include explicit `admin_grant` dependencies for admin-gated operations

**DON'T:**
- Add boolean flags to skip authorization checks
- Create "bootstrap special cases" that bypass normal flow
- Trust implicit authorization (always verify signatures)

### 4. Consistent Patterns

All event types follow the same pattern:

```python
def create(...) -> str:
    """Create event and store via store.event()."""
    event_data = {...}
    signed_event = crypto.sign_event(event_data, private_key)
    blob = crypto.canonicalize_json(signed_event)
    return store.event(blob, peer_id, t_ms, db)

def project(event_id, recorded_by, recorded_at, db) -> str | None:
    """Project event into table. Return event_id on success, None on failure."""
    # Get blob, verify signature, insert into table
    ...
```

**DO:**
- Follow the standard create/project pattern
- Return `event_id` on success, `None` on failure from projectors
- Use `store.event()` which handles `recorded.create()` and `recorded.project()`

**DON'T:**
- Create non-standard projector signatures
- Skip the `store.event()` flow
- Return different values from projectors

## Common Gotchas

### Gotcha 1: Timestamp Offsets

**Wrong:**
```python
group_id = group.create(t_ms=t_ms + 10, ...)
member_id = group_member.create(t_ms=t_ms + 20, ...)
```

**Right:**
```python
group_id = group.create(t_ms=t_ms, ...)
member_id = group_member.create(t_ms=t_ms, ...)  # DAG deps handle ordering
```

Timestamps are for Last-Writer-Wins queries, not ordering. The DAG handles ordering via explicit dependencies.

### Gotcha 2: Redundant Projection

**Wrong:**
```python
event_id = some_event.create(...)
recorded_id = recorded.create(event_id, peer_id, t_ms, db, return_dupes=True)
recorded.project_ids([recorded_id], db)  # REDUNDANT
```

**Right:**
```python
event_id = some_event.create(...)  # store.event() already projects
```

`some_event.create()` calls `store.event()` which calls `recorded.project()`. No need for explicit projection.

### Gotcha 3: Authorization Bypass Flags

**Wrong:**
```python
def create(..., skip_admin_check=False):
    if not skip_admin_check and not is_admin(...):
        raise ValueError("Not authorized")
```

**Right:**
```python
def create(...):
    if not is_admin(...):
        raise ValueError("Not authorized")
```

If you think you need a bypass, the real problem is that dependencies aren't projected yet. Fix the ordering, don't add bypasses.

### Gotcha 4: Placeholder Strings

**Wrong:**
```python
peer_shared_id = 'PENDING'  # Magic string
signed_by = 'SELF'  # Magic string
```

**Right:**
```python
peer_shared_id = None  # Not available yet
# Network events omit signed_by entirely - they're self-signed
```

Use `None` for missing values. Network events are self-signed using `network_pubkey` from the event body.

### Gotcha 5: Direct Table Inserts

**Wrong:**
```python
safedb.execute("INSERT INTO users ...", (...))  # Bypasses event-sourcing
```

**Right:**
```python
user_id = user.create(...)  # Creates event, projects to table
```

Always go through the event system. Direct inserts can't be reprojected and break consistency.

### Gotcha 6: Store Blob Fallbacks

**Wrong:**
```python
# Try table first, fall back to store blob
row = safedb.query_one("SELECT * FROM invites WHERE ...")
if not row:
    blob = store.get(invite_id, db)  # Bootstrap fallback
```

This pattern masks missing dependencies. If you need data from a table, the event should have been projected first. Fix the dependency chain.

**Exception:** Crypto verification during projection may need to fall back to store blob when the signer's event hasn't projected yet. This is acceptable for signature verification only.

### Gotcha 7: Implicit Dependencies

**Wrong:**
```python
def project(event_id, recorded_by, recorded_at, db):
    # Query a table without declaring it as a dependency
    user_row = safedb.query_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
    if not user_row:
        return None  # Silently fails, no blocking
```

**Right:**
```python
# In check_deps() or event data:
# Declare user_id as an explicit dependency field
event_data = {'user_id': user_id, ...}  # check_deps() will verify this
```

If your projector queries a table and returns `None` when data is missing, that's an implicit dependency. Make it explicit so `check_deps()` can block properly.

### Gotcha 8: Inappropriate unsafedb Usage

**Wrong:**
```python
# Using unsafedb to see what other peers see
unsafedb = create_unsafe_db(db)
row = unsafedb.query_one("SELECT * FROM users WHERE ...")  # Sees ALL peers' data
```

**Right:**
```python
# Use safedb scoped to this peer's view
safedb = create_safe_db(db, recorded_by=recorded_by)
row = safedb.query_one("SELECT * FROM users WHERE ... AND recorded_by = ?", (..., recorded_by))
```

`unsafedb` bypasses `recorded_by` scoping. Only use it for:
- Store access (`store.get()`)
- Device-wide tables (not per-peer)
- Explicit cross-peer queries where documented

Default to `safedb` for all projection table queries.

### Gotcha 9: Non-Event Side Effects

**Wrong:**
```python
def some_function(...):
    # Do something that changes state but isn't an event
    safedb.execute("UPDATE some_table SET field = ? WHERE ...", (...))
```

**Right:**
```python
def some_function(...):
    # Create an event that expresses the state change
    event_id = state_change_event.create(...)
    # Projection handles the table update
```

All state changes must be expressed as events. Side effects that bypass events can't be replayed or synced.

### Gotcha 10: Incorrect Key Hinting

**Wrong:**
```python
# Using wrong value as key hint
blob = crypto.wrap(plaintext, {'id': some_other_id, 'key': key_bytes, 'type': 'symmetric'}, db)
```

**Right:**
```python
# key_id IS the event_id (content-addressed)
blob = crypto.wrap(plaintext, {'id': crypto.b64decode(key_id), 'key': key_bytes, 'type': 'symmetric'}, db)
```

In content-addressed systems, `key_id = event_id`. The hint must match so decryption can find the right key.

**Pattern:** Always create keys using their provided `create()` function (e.g., `group_key.create()`, `transit_key.create()`). These functions return the `event_id`, which is the correct value to use as the key hint. Don't generate key IDs separately—let the event system provide them.

### Gotcha 11: Direct Key Insertion

**Wrong:**
```python
# Inserting key material directly into table
safedb.execute("INSERT INTO group_keys (key_id, key, ...) VALUES (?, ?, ...)", (key_id, key_bytes, ...))
```

**Right:**
```python
# Create deterministic key event from material
key_id = group_key.create_with_material(key_bytes, peer_id, t_ms, db)
# Projection inserts into group_keys table
```

Keys should be created as events using `create_with_material()`. This ensures:
- Deterministic key_id (same material = same ID everywhere)
- Proper event-sourcing
- Natural projection flow

### Gotcha 12: Cross-Event-Type Table Access

**Wrong:**
```python
# In message.py, directly writing to channels table
safedb.execute("UPDATE channels SET last_message_at = ?", (t_ms,))
```

**Right:**
```python
# Each event type controls its own tables
# If you need to update another type's state, create an appropriate event
# Or use an explicitly provided interface from that module
```

Event types should only write to tables they control (as declared in `PROJECTION_TABLE`). To interact with other event types' data:
- Use their provided query functions (e.g., `channel.get_channel()`)
- Create events that trigger their projectors
- Don't reach into their tables directly

### Gotcha 13: Direct Table Queries Across Boundaries

**Wrong:**
```python
# In invite.py, directly querying group_members table
members = safedb.query("SELECT * FROM group_members WHERE group_id = ?", (group_id,))
```

**Right:**
```python
# Use the interface provided by group_member module
from events.group import group_member
members = group_member.list_members(group_id, recorded_by, db)
```

Event modules should provide clear interfaces for querying their data. This:
- Encapsulates table structure
- Ensures consistent filtering (e.g., excluding removed users)
- Makes refactoring easier

See `events/network/sync.py` connection handling for a good example of interface design.

### Gotcha 14: Missing admin_grant Dependency

**Wrong:**
```python
# Admin-gated event without explicit dependency
event_data = {
    'type': 'group_member',
    'group_id': group_id,
    'added_by': peer_shared_id,  # Must be admin, but how do receivers verify?
}
```

**Right:**
```python
event_data = {
    'type': 'group_member',
    'group_id': group_id,
    'added_by': peer_shared_id,
    'admin_grant': admin_grant_id,  # Explicit dependency - receivers can verify
}
```

For admin-gated operations, include the `admin_grant` event ID. This ensures:
- Receivers can verify authorization via the event chain
- Events project correctly regardless of arrival order
- No runtime "is this person an admin?" checks that might give different answers

### Gotcha 15: Missing Signature Verification

**Wrong:**
```python
def project(event_id, recorded_by, recorded_at, db):
    blob = store.get(event_id, db)
    event_data = crypto.parse_json(blob)
    # Just use the data without verifying signature
    safedb.execute("INSERT INTO ...", (...))
```

**Right:**
```python
def project(event_id, recorded_by, recorded_at, db):
    blob = store.get(event_id, db)
    event_data = crypto.parse_json(blob)

    # Verify signature before trusting data
    public_key = get_signer_public_key(event_data['signed_by'], recorded_by, db)
    if not crypto.verify_event(event_data, public_key):
        log.warning("Signature verification failed")
        return None

    # Now safe to use
    safedb.execute("INSERT INTO ...", (...))
```

Always verify signatures in projectors. Unverified events could be forged.

## Event Registry

Event types are registered in `events/__init__.py`. Each event module exports:

```python
EVENT_TYPE = 'event_name'
SHAREABLE = True/False  # Does this sync to other peers?
EPHEMERAL = False       # Is this a one-shot event (no persistence)?
PROJECTION_TABLE = ('table_name', 'id_column')  # Or None if no table
```

The registry enables:
- Auto-discovery of projectors via `registry.get_project_fn(event_type)`
- Centralized shareable/ephemeral checks
- Consistent event type handling

## Testing

See `tests/RULES.md` for test-specific guidelines. Key points:

- Use deterministic `t_ms` values, never `time.time()`
- Use `tick()` to drive sync operations
- Test via API queries, not direct table inspection
- Assert convergence in multi-peer scenarios

### Scenario Tests: Always Use `assert_eventually`

**Wrong:**
```python
run_ticks(db=db, start_t_ms=5000, num_rounds=200)
assert message_synced_to_bob(db)  # May fail if sync takes longer
```

**Right:**
```python
from tests.utils.tick_helper import assert_eventually

assert_eventually(
    lambda: message_synced_to_bob(db),
    db=db,
    start_t_ms=5000,
    msg="Message should sync to Bob"
)
```

`assert_eventually` runs ticks and retries until the condition passes or times out. This avoids flaky tests that depend on exact tick counts for sync convergence. Use it for all assertions that depend on sync/projection completing.
