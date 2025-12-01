# Plan: Extending Pure Projectors for Complex Events

## Problem Statement

Two event types resist the pure projector pattern due to complex side effects:

1. **group_key_shared** - Creates derived events, queue interactions
2. **message_deletion** - Authorization queries against message author/admin status

This document analyzes the limitations and proposes generalizations to handle them naturally.

---

## Analysis: group_key_shared

### Current Complexity

```python
def project(key_shared_id, recorded_by, recorded_at, db):
    # 1. Decrypt (if not for us, return None - already handled by resolve())
    # 2. Verify signature
    # 3. CREATE DERIVED EVENT: group_key.create_with_material(symmetric_key, ...)
    # 4. Verify key_id matches (security check)
    # 5. Insert into group_keys_shared
    # 6. QUEUE INTERACTION: queues.blocked.notify_event_valid(computed_key_id, ...)
    # 7. Re-project unblocked events
```

### Why It Seems Hard

1. **Derived event creation** - `group_key.create_with_material()` creates a new event
2. **Queue notification** - Must notify events waiting on `key_id`
3. **Recursive projection** - Must re-project unblocked events

### User's Insight: It's Actually Simple

**Key insight #1**: Outputting derived events is a standard functional output.

```python
@dataclass
class ProjectorResult:
    valid: bool = True
    reason: str | None = None
    tables: dict[str, list[dict]] = field(default_factory=dict)
    blocked: bool = False
    missing_deps: list[str] = field(default_factory=list)
    # NEW: Events to create as side effect
    derived_events: list[dict] = field(default_factory=list)
```

**Key insight #2**: Key blocking IS normal event-id blocking.

Events encrypted with `key_id` are blocked waiting for that `key_id` to appear in `valid_events`. When `group_key_shared` projects:
1. It creates `group_key` event with that `key_id`
2. `group_key` gets marked valid → triggers `notify_event_valid(key_id, ...)`
3. Events waiting on that key_id get unblocked automatically

The queue interaction isn't special - it's the same mechanism as any other event dependency!

### Proposed Pure Projector

```python
# projectors/group_key_shared.py

SPEC = {
    "encrypted": True,  # Wrapped to recipient prekey
    "signer_type": "peer_shared",
    "dependencies": [],
    "tables": ["group_keys_shared"],
}

def project(input_dict) -> ProjectorResult:
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]
    event_id = input_dict["event_id"]

    original_key_id = event_data["key_id"]
    symmetric_key = crypto.b64decode(event_data["symmetric_key"])

    # Compute deterministic key_id from key material
    computed_key_id = crypto.hash_key_material(symmetric_key)

    # Security check: key_id must match
    if computed_key_id != original_key_id:
        return ProjectorResult(valid=False, reason="key_id mismatch - possible tampering")

    # Output: group_keys_shared row
    gks_row = {
        "key_shared_id": event_id,
        "original_key_id": computed_key_id,
        "signed_by": event_data["signed_by"],
        "created_at": event_data["created_at"],
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    # Output: derived group_key event to create
    derived_group_key = {
        "type": "group_key",
        "key_id": computed_key_id,
        "symmetric_key": symmetric_key,  # Raw bytes
        "created_at": event_data["created_at"],
    }

    return ProjectorResult(
        valid=True,
        tables={"group_keys_shared": [gks_row]},
        derived_events=[derived_group_key],
    )
```

### Framework Changes for Derived Events

```python
# projectors/project.py

def apply_result(result: ProjectorResult, recorded_by: str, recorded_at: int, db: Any) -> bool:
    if result.blocked or not result.valid:
        return False

    # Apply table writes
    for table_name, rows in result.tables.items():
        # ... existing INSERT OR IGNORE logic ...

    # NEW: Create derived events
    for derived in result.derived_events:
        event_type = derived["type"]
        if event_type == "group_key":
            from events.group import group_key
            group_key.create_with_material(
                derived["symmetric_key"],
                recorded_by,
                derived["created_at"],
                db
            )
            # create_with_material already marks it valid
            # notify_event_valid happens automatically in recorded.py

    return True
```

---

## Analysis: message_deletion

### Current Complexity

```python
def project(deletion_id, recorded_by, recorded_at, db):
    # 1. Decrypt, parse
    # 2. Get message_id, deleted_by from event
    # 3. AUTHORIZATION QUERY: Look up message author_id
    # 4. AUTHORIZATION CHECK: validate(message_id, deleted_by, recorded_by, db)
    #    - Author can delete own messages
    #    - Admin can delete any message in their group
    # 5. Insert into message_deletions
    # 6. Mark key for purging (forward secrecy)
```

### Why It Seems Hard

1. **Authorization requires message lookup** - Need `author_id` from messages table
2. **Authorization requires admin check** - Need to verify deleter is admin
3. **Pre-block case** - Deletion can arrive before message

### User's Insight: Use Dependencies

**Key insight #1**: Authorization is a dependency relationship.

The `validate()` function checks:
```python
def validate(message_id, deleted_by, recorded_by, db):
    # Get message author
    message = query("SELECT author_id, group_id FROM messages WHERE message_id = ?")

    # Author can delete own
    if get_user_id(deleted_by) == message.author_id:
        return True

    # Admin can delete any in group
    if is_admin(deleted_by, message.group_id):
        return True

    return False
```

This can be expressed as dependencies:
- `signer_user:linked_peer` - Get deleter's user_id
- `message:message_lookup?` - Get message author_id and group_id (optional - may not exist yet)
- `signer_admin:admin_in_group?` - Check if deleter is admin in message's group

**Key insight #2**: "Unblocked implies valid" for admin_grant.

If we block on `admin_grant`, and it gets unblocked, that means `admin_grant` is valid. We don't need to re-query - the blocking mechanism provides the guarantee.

### Proposed Pure Projector

```python
# projectors/message_deletion.py

SPEC = {
    "encrypted": True,
    "signer_type": "peer_shared",
    "dependencies": [
        "signer_user:linked_peer",        # Deleter's user_id
        "message:message_lookup?",        # Message author (optional - pre-block)
        "signer_admin:admin_for_message?", # Admin grant for message's group (optional)
    ],
    "tables": ["message_deletions", "deleted_events", "keys_to_purge"],
}

def project(input_dict) -> ProjectorResult:
    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]
    deps = input_dict["dependencies"]
    key_id = input_dict.get("key_id")  # From encrypted blob

    message_id = event_data["message_id"]
    deleted_by = event_data["signed_by"]
    created_at = event_data["created_at"]

    signer_user = deps.get("signer_user")
    message_info = deps.get("message")
    signer_admin = deps.get("signer_admin")

    # Authorization check
    if message_info:
        # Message exists - strict authorization
        author_id = message_info["author_id"]
        signer_user_id = signer_user["user_id"] if signer_user else None

        is_author = (signer_user_id == author_id)
        is_admin = (signer_admin is not None)  # Unblocked = valid

        if not is_author and not is_admin:
            return ProjectorResult(
                valid=False,
                reason=f"Not authorized: {deleted_by} is neither author nor admin"
            )
    else:
        # Message doesn't exist yet - accept as pre-block
        # Authorization will be checked when message tries to project
        # (message.project checks deleted_events table)
        pass

    # Outputs
    tables = {
        "message_deletions": [{
            "deletion_id": event_id,
            "message_id": message_id,
            "deleted_by": deleted_by,
            "created_at": created_at,
            "recorded_by": recorded_by,
            "recorded_at": recorded_at,
        }],
        "deleted_events": [{
            "event_id": message_id,
            "recorded_by": recorded_by,
        }],
    }

    # Forward secrecy: mark key for purging
    if key_id:
        tables["keys_to_purge"] = [{
            "key_id": key_id,
            "marked_at": recorded_at,
            "recorded_by": recorded_by,
        }]

    return ProjectorResult(valid=True, tables=tables)
```

### New Dependency Types Needed

```python
# In projectors/project.py _resolve_dependency()

elif dep_type == "message_lookup":
    # Look up message for deletion authorization
    message_id = event_data.get("message_id")
    if message_id:
        row = safedb.query_one(
            "SELECT author_id, group_id FROM messages WHERE message_id = ? AND recorded_by = ?",
            (message_id, recorded_by)
        )
        if row:
            result = {"author_id": row["author_id"], "group_id": row["group_id"]}

elif dep_type == "admin_for_message":
    # Check if signer is admin for the message's group
    # First need the message to get group_id
    message_id = event_data.get("message_id")
    signed_by = event_data.get("signed_by")
    if message_id and signed_by:
        # Get message's group
        msg_row = safedb.query_one(
            "SELECT group_id FROM messages WHERE message_id = ? AND recorded_by = ?",
            (message_id, recorded_by)
        )
        if msg_row:
            group_id = msg_row["group_id"]
            # Get signer's user_id
            peer_row = safedb.query_one(
                "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
                (signed_by, recorded_by)
            )
            if peer_row and peer_row["user_id"]:
                # Check if user is admin in that group
                admin_row = safedb.query_one(
                    """SELECT admin_id FROM admins
                       WHERE user_id = ? AND group_id = ? AND recorded_by = ?""",
                    (peer_row["user_id"], group_id, recorded_by)
                )
                if admin_row:
                    result = {"admin_id": admin_row["admin_id"]}
```

---

## Summary: Pattern Generalizations

### 1. Derived Events as Standard Output

Add `derived_events: list[dict]` to `ProjectorResult`. The framework creates them after applying table writes.

```python
@dataclass
class ProjectorResult:
    valid: bool = True
    tables: dict[str, list[dict]] = field(default_factory=dict)
    derived_events: list[dict] = field(default_factory=list)  # NEW
```

### 2. Key Blocking = Event Blocking

No special queue handling needed. When `group_key_shared` creates a `group_key` event:
- `group_key` becomes valid
- `recorded.py` calls `notify_event_valid(key_id, ...)`
- Events waiting on that key_id unblock automatically

### 3. Authorization via Dependencies

Instead of querying during projection, declare dependencies:
- `message_lookup?` - Get message info for deletion auth
- `admin_for_message?` - Check admin status for message's group
- Blocking guarantees validity - "unblocked = valid"

### 4. Dependency Resolution Helpers

Add new dependency types to `_resolve_dependency()`:
- `message_lookup` - Returns `{author_id, group_id}` for a message
- `admin_for_message` - Returns `{admin_id}` if signer is admin for message's group

---

## Implementation Status

### group_key_shared: ✅ IMPLEMENTED

Both insights validated:

1. **derived_events works** - Pure projector returns `derived_events` list, `apply_result()`
   creates the group_key via `create_with_material()`.

2. **Key blocking = event blocking validated** - When `apply_result()` creates the derived
   group_key, `store.event()` → `recorded.project()` → `notify_event_valid(key_id, ...)`
   triggers automatically. No manual queue handling needed in the projector wrapper.

Key implementation detail: The event module keeps the `unwrap_event()` and signature
verification (these have special requirements), then calls the pure projector for just
the computation.

### message_deletion: ✅ IMPLEMENTED

Implemented using a different approach than originally planned. Instead of the complex
"same-transaction enhancement" with `find_blocked_deletion_for()`, we use:

1. **Generic `deleted_events` table** - Framework manages deletion tracking
2. **`cleanup_deleted_events()` at end of transaction** - Cascades deletions and cleans data
3. **Pure projector just does authorization** - Outputs `deleted_events=[message_id]`

The "same-transaction" behavior happens automatically:
- Message projects → `notify_event_valid(message_id)` → deletion unblocks
- Deletion projects immediately (recursive call in same stack)
- `cleanup_deleted_events()` runs before transaction commits
- Message never visible externally

See `docs/deletion-framework-design.md` for full design rationale.

---

## Revised Analysis: message_deletion Ordering

### The Problem

When a deletion event arrives before the message it deletes:
1. We can't verify authorization (don't know if deleter is author/admin)
2. If we accept the deletion anyway, and later the message arrives, we need to
   check if the deletion was valid
3. If the message projects before the deletion is validated, users see a "flash"
   of the message before it appears deleted

### Options Explored

#### Option 1: Message projector checks "am I deleted?"
```python
def project(input_dict):
    if is_deleted(message_id, recorded_by, db):  # DB query
        return ProjectorResult(valid=True, tables={})
```
- **Pros**: Simple
- **Cons**: Not a pure projector (DB query inside)

#### Option 2: Framework passes deletion info as dependency
```python
SPEC = {"dependencies": ["deletion:pending_deletion?"]}
def project(input_dict):
    if input_dict["dependencies"].get("deletion"):
        return ProjectorResult(valid=True, tables={})
```
- **Pros**: Projector stays pure
- **Cons**: Deletion must project first, doesn't truly depend on message

#### Option 3: Deletion depends on message, process in same transaction
- Deletion blocks until message exists (normal dependency)
- When message projects, check for unblocked deletions targeting it
- Process deletion in same transaction → message never visible if deleted
- **Pros**: Correct dependency semantics, no flash
- **Cons**: Complex transaction handling

#### Option 4: recorded.py intercepts deleted messages
- Deletion projects immediately to `deleted_events`
- `recorded.project()` checks `deleted_events` before dispatching to message projector
- **Pros**: Framework-level, projector stays pure
- **Cons**: Deletion doesn't depend on message

### Chosen Approach: Option 3 (Two-Layer)

**Key insight**: Separate the base model from the UX enhancement.

**Base model** (simple, correct):
- Deletion depends on the deleted event (message_id is a blocking dependency)
- Normal blocking/unblocking via `valid_events`
- When message projects → deletion unblocks → deletion projects
- Semantically correct, possible visible flash

**Enhancement** (better UX):
- When projecting message M, detect "did this unblock a deletion for M?"
- If yes, process the deletion in the same transaction
- Message is never visible in non-deleted state

This is clean because:
1. The dependency model stays pure and simple
2. The enhancement is an optimization layer, not a semantic change
3. Without the enhancement, everything still works (just suboptimal UX)

### Implementation Plan

#### Phase 1: Base Model
1. Deletion SPEC declares `message_id` as a blocking dependency
2. Deletion blocks until message is valid
3. When message becomes valid, deletion unblocks and projects normally
4. Authorization check happens in deletion projector (message info available as dep)

```python
# projectors/message_deletion.py
SPEC = {
    "encrypted": True,
    "signer_type": "peer_shared",
    "dependencies": [
        "message:message_event",      # BLOCKING - wait for message
        "signer_user:linked_peer",    # Get deleter's user_id
    ],
    "tables": ["message_deletions", "deleted_events"],
}

def project(input_dict) -> ProjectorResult:
    message_info = input_dict["dependencies"]["message"]
    signer_user = input_dict["dependencies"]["signer_user"]

    # Authorization: is signer the author or an admin?
    is_author = (signer_user["user_id"] == message_info["author_id"])
    is_admin = check_admin(signer_user["user_id"], message_info["group_id"])

    if not is_author and not is_admin:
        return ProjectorResult(valid=False, reason="Not authorized")

    return ProjectorResult(valid=True, tables={...})
```

#### Phase 2: Same-Transaction Enhancement
```python
# In recorded.project(), after projecting message
if event_type == 'message':
    # Check: did we unblock a deletion targeting this message?
    pending_deletion = find_blocked_deletion_for(ref_id, recorded_by, db)
    if pending_deletion:
        # Process deletion in same transaction
        dispatch('message_deletion', pending_deletion, recorded_by, recorded_at, db)
        # Message is now deleted before tx commits
```

The `find_blocked_deletion_for()` queries the blocked queue for deletion events
where `message_id == ref_id`.

---

## Implementation Steps

### Completed ✅

1. **Add `derived_events` to ProjectorResult**
   - Updated dataclass in `project.py`
   - Updated `apply_result()` to create derived events

2. **Convert group_key_shared**
   - Created pure projector with `derived_events` output
   - Verified key blocking works through normal event blocking
   - Removed manual queue notification (automatic via recorded.py)

3. **Implement message_deletion**
   - Added `deleted_events` field to ProjectorResult
   - Added `message_event` dependency type to `_resolve_dependency()`
   - Created `cleanup_deleted_events()` with `DATA_TABLES` registry
   - Pure projector handles authorization, outputs `deleted_events=[message_id]`
   - Same-transaction behavior via cleanup before commit (no special enhancement needed)

### Remaining

4. **Convert remaining event types to pure projectors**
   - sync_connect - updates sync state table
   - sync - generates response, updates outgoing queue
   - file_slice - content slice handling
   - message_attachment - attachment metadata
   - network_address - network discovery
   - network_intro - network introduction
   - Local-only types (peer, transit_key, group_key, prekeys, network_created)

---

## Benefits

1. **Pure projectors remain pure** - No DB queries in project(), all via dependencies
2. **Composition** - Dependencies are declared in SPEC, resolved by framework
3. **Testability** - Can unit test with mock dependencies
4. **Clarity** - Authorization logic is explicit in the pure function
5. **Blocking guarantees** - Unblocked = valid, no need to re-check
6. **Correct dependency semantics** - Deletion truly depends on message
7. **UX enhancement is separate** - Base model works without same-transaction optimization
