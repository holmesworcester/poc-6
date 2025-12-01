# Deletion Framework Design

This document captures the design decisions for the generic deletion framework.

## Problem Statement

We need to handle deletion of events (messages, reactions, etc.) in a way that:
1. Cascades to dependent events (e.g., reactions to a deleted message)
2. Handles deletions that arrive before their target (via sync)
3. Cleans up projected data in tables
4. Is extensible to new deletion types

## Options Considered

### Option 1: Deletion Blocks on Target (Original Plan)

- Deletion event blocks until target message is valid
- Projector validates authorization using message data
- Cascade runs during deletion projection

**Problems:**
- Cascade logic duplicated in each deletion projector
- "Pre-block" case (deletion arrives before message) requires special handling
- Each deletion type needs its own cascade implementation

### Option 2: Per-Type Deletion Tables

- `message_deletions` table for messages
- `user_deletions` table for users (future)
- Each projector manages its own cascade

**Problems:**
- Multiple tables doing similar things
- Cascade logic duplicated
- Hard to extend to new deletion types

### Option 3: Generic `deleted_events` with Framework Cascade (CHOSEN)

- Single `deleted_events` table managed by framework
- Pure projector outputs `deleted_events=[event_id]`
- Framework handles ALL cascade logic at end of transaction
- Audit trail lives in the deletion event blob itself

**Benefits:**
- Single cascade implementation for all deletion types
- Pure projectors are minimal (just authorization)
- New deletion types trivial to add
- Future reactions handled automatically
- Order-independent within transaction

## Chosen Design

### Core Principle

Deletion is infrastructure, not business logic. The framework handles:
1. Recording what's deleted (`deleted_events` table)
2. Removing from validity tracking (`valid_events`)
3. Cascading to dependents (using `event_dependencies`)
4. Cleaning up projected data (using existing event_id columns via DATA_TABLES registry)

The pure projector only handles authorization.

### Key Insight: No Separate Deletion Tables

We considered having a `message_deletions` table to track deleted messages, but realized:
- The deletion event blob itself is the audit trail
- A generic `deleted_events` table works for all deletion types
- Existing event_id columns (message_id, reaction_id, etc.) can be used for cleanup

### How Cascade Works

The `cleanup_deleted_events()` function runs at end of transaction:

1. **Phase 1: Cascade loop** - Repeatedly removes from `valid_events`:
   - Events directly in `deleted_events`
   - Orphaned dependents (parent not in `valid_events`)
   - Loop continues until no more removals

2. **Phase 2: Data cleanup** - Uses `DATA_TABLES` registry to remove projected data:
   ```python
   DATA_TABLES = {
       'messages': 'message_id',
       'channels': 'channel_id',
       ...
   }
   ```
   For each table, deletes rows where event_id column not in `valid_events`.

### How Future Dependencies Are Caught

**Scenario: Reaction arrives after message deleted**

1. Message deleted → `deleted_events` contains message_id
2. Cleanup runs → message_id removed from `valid_events`
3. Later: Reaction arrives via sync
4. Reaction projects → added to `valid_events`, `event_dependencies` records parent=message_id
5. End of transaction: cleanup runs again
6. Cleanup finds reaction's parent (message_id) not in valid_events
7. Reaction removed from valid_events
8. Reaction's projected data removed via DATA_TABLES cleanup

The same cascade loop handles all cases uniformly.

### Pure Projector Pattern

```python
# projectors/message_deletion.py
SPEC = {
    "encrypted": True,
    "signer_type": "peer_shared",
    "dependencies": ["message:message_event"],  # name:type format
    "tables": [],  # No type-specific table needed!
}

def project(input_dict) -> ProjectorResult:
    # Authorization check only
    message = deps.get("message")
    if not message:
        return ProjectorResult(blocked=True, missing_deps=["message"])

    is_author = (deleted_by == message.get("author_id"))
    is_admin = deps.get("is_admin", False)

    if not is_author and not is_admin:
        return ProjectorResult(valid=False, reason="not authorized")

    return ProjectorResult(valid=True, deleted_events=[message_id])
```

The wrapper handles type-specific side effects (forward secrecy key purging, blob deletion).

## Why This Design

1. **Single source of truth**: `deleted_events` + `valid_events` invariant
2. **Generic cascade**: One implementation handles all deletion types
3. **Pure projectors minimal**: Just authorization, output `deleted_events=[id]`
4. **Future-proof**: New deletion types need only a projector
5. **Consistent**: All edge cases (future deps, same-transaction, nested chains) handled uniformly
6. **Testable**: Framework testable once, projectors testable in isolation

## Implementation Notes

### Bug Fix: db.changes()

During implementation, discovered that `Database.changes()` was using SQLite's
`total_changes` (cumulative since connection open) instead of `SELECT changes()`
(rows affected by last statement). This caused an infinite loop in the cascade.

Fixed by changing to `SELECT changes()`.

### Files Changed

- `projectors/project.py` - Added `deleted_events` field, `DATA_TABLES` registry, `cleanup_deleted_events()`
- `projectors/message_deletion.py` - New pure projector (authorization only)
- `projectors/__init__.py` - Exported new functions
- `events/content/message_deletion.py` - Updated wrapper to use pure projector
- `db.py` - Fixed `changes()` implementation
