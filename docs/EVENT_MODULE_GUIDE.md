# Event Module Standards Guide

This document standardizes the structure and patterns used across all 40+ event modules in poc-6.

## Module Structure

Every event module should follow this template:

```python
"""Brief description of event type and domain.

More detailed explanation of:
- What this event represents
- What state it manages
- Key invariants or constraints
"""
from typing import Any
import logging
from db import create_safe_db, create_unsafe_db
import crypto
import store

log = logging.getLogger(__name__)

# Constants specific to this event type
SOME_CONSTANT = value
```

## Required Functions

Each event module must implement these three core functions:

### 1. `create()` Function

**Purpose**: Create a new event of this type

**Signature**:
```python
def create(
    peer_id: str,
    # ... event-specific parameters ...
    t_ms: int,
    db: Any
) -> dict[str, Any]:
```

**Implementation Pattern**:
```python
def create(peer_id: str, foo: str, bar: int, t_ms: int, db: Any) -> dict[str, Any]:
    """Create event with specific purpose.

    Args:
        peer_id: Peer creating this event
        foo: Specific field
        bar: Specific field
        t_ms: Timestamp
        db: Database connection

    Returns:
        Dict with created event info (event_id, etc.)
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Step 1: Validate inputs and access
    # - Check required fields
    # - Verify peer has access to resources being referenced
    # - Log clearly what's being created

    # Step 2: Build event data
    event_data = {
        'type': 'event_type_name',
        'field1': value1,
        'field2': value2,
        # ... all fields for this event
    }

    # Step 3: Sign if needed
    # - Most events are signed with a key to prevent tampering
    # - The signing key depends on event type (peer, group, invite key, etc.)
    # - Check similar event modules for the appropriate signing strategy
    if needs_signing:
        signing_key = get_appropriate_key(...)
        signed_event = crypto.sign_event(event_data, signing_key)
    else:
        signed_event = event_data

    # Step 4: Canonicalize and encrypt
    canonical = crypto.canonicalize_json(signed_event)
    key_data = group.pick_key(group_id, peer_id, db)  # If group-encrypted
    blob = crypto.wrap(canonical, key_data, db)

    # Step 5: Store
    event_id = store.event(blob, peer_id, t_ms, db)

    # Step 6: Return metadata
    return {
        'id': event_id,
        'field1': value1,
        # ... any useful return data
    }
```

**Key Points**:
- Always use `safedb = create_safe_db(db, recorded_by=peer_id)` for access control
- Validate all inputs and dependencies upfront
- Build event_data dict with 'type' field
- Sign if needed (key type depends on event domain - check similar modules)
- Wrap with appropriate encryption key (if needed)
- Store once and return event_id
- Log creation at completion

### 2. `project()` Function

**Purpose**: Project event onto persistent storage (DB tables)

**Signature**:
```python
def project(event_id: str, event_data: dict[str, Any], recorded_by: str, recorded_at: int, db: Any) -> None:
```

**Implementation Pattern**:
```python
def project(event_id: str, event_data: dict[str, Any], recorded_by: str, recorded_at: int, db: Any) -> None:
    """Project event into tables for query access.

    Args:
        event_id: Event ID
        event_data: Decrypted/unwrapped event data
        recorded_by: Peer who recorded this event
        recorded_at: Timestamp when recorded
        db: Database connection
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Step 1: Extract fields from event_data with defaults
    field1 = event_data.get('field1')
    field2 = event_data.get('field2')

    # Step 2: Validate fields - check AFTER extraction
    if not all([field1, field2]):
        log.warning(f"project() missing required fields: {list(event_data.keys())}")
        return

    # Step 3: Validate business logic (if needed)
    # - Verify signature if event is signed
    # - Check that referenced events exist (dependency validation)
    # - Check that peer has permission to perform this action
    # - The specific validations depend on event type

    if event_data.get('needs_signature_check'):
        # Most events that are signed include a 'signed_by' field
        # Verify with appropriate key type (peer_shared, group key, etc.)
        if not crypto.verify_signed_by_peer_shared(event_data, recorded_by, db):
            log.warning(f"project() signature verification failed")
            return

    # Step 4: Insert into tables
    safedb.execute(
        """INSERT OR IGNORE INTO table_name
           (event_id, field1, field2, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?)""",
        (event_id, field1, field2, recorded_by, recorded_at)
    )

    # Step 5: Record dependencies if applicable
    # - For cascading deletions
    # - For integrity constraints
    if parent_event_id:
        safedb.execute(
            """INSERT OR IGNORE INTO event_dependencies
               (child_event_id, parent_event_id, recorded_by, dependency_type)
               VALUES (?, ?, ?, ?)""",
            (event_id, parent_event_id, recorded_by, 'parent_type')
        )

    log.debug(f"project() projected event {event_id[:20]}...")
```

**Key Points**:
- Always create safedb with `recorded_by` for access control
- Extract fields with `.get()` method (returns None if missing)
- Validate all fields exist before proceeding
- Use `INSERT OR IGNORE` (idempotent)
- Check signatures if event includes a 'signed_by' field (verify with appropriate key)
- Record dependencies for cascading operations
- Return silently on validation failure (event will remain blocked)

### 3. `dependencies()` Function

**Purpose**: Return list of event IDs that this event depends on

**Signature**:
```python
def dependencies(event_data: dict[str, Any]) -> list[str]:
```

**Implementation Pattern**:
```python
def dependencies(event_data: dict[str, Any]) -> list[str]:
    """Return list of dependencies.

    Args:
        event_data: Event data dict

    Returns:
        List of event IDs that must be projected before this event
    """
    deps = []

    # Parent event dependencies
    if parent_id := event_data.get('parent_id'):
        deps.append(parent_id)

    # Peer dependencies
    if peer_shared_id := event_data.get('peer_shared_id'):
        deps.append(peer_shared_id)

    # Group key dependencies (if needed)
    if key_id := event_data.get('key_id'):
        deps.append(key_id)

    return deps
```

**Key Points**:
- Return list of event_id strings that must be projected first
- Use walrus operator `:=` for cleaner code
- Return empty list if no dependencies
- The dependency system handles the rest (blocks events until deps exist)

## Field Naming Conventions

### Standard Fields (most events)

| Field | Type | Description |
|-------|------|-------------|
| `type` | str | Event type name (e.g., 'message', 'group_key') |
| `signed_by` | str (base64) | Identity of signer (peer_shared_id, key_id, etc.) - if signed |
| `created_at` | int | Timestamp when event was created (t_ms) |

### Common Optional Fields

| Field | Type | Description | Used In |
|-------|------|-------------|---------|
| `parent_id` | str | Event this one relates to | deletions, edits |
| `peer_shared_id` | str | Public peer identity | invites, peer_shared |
| `key_id` | str | Encryption key ID | encrypted content |
| `nonce_prefix` | str (base64) | Nonce prefix for encryption | files |
| `recorded_by` | str | Peer who recorded event | metadata |

### Naming Rules

- Use `_id` suffix for event IDs
- Use `_shared_id` for public peer identities
- Use `_at` suffix for timestamps
- Use `_ms` for millisecond values
- Use snake_case for field names
- Use `b64` or `_b64` suffix for base64-encoded values

## Database Table Patterns

### Pattern 1: Simple Projection

For events that just store their data:

```sql
CREATE TABLE table_name (
    event_id TEXT PRIMARY KEY,
    field1 TEXT,
    field2 INTEGER,
    recorded_by TEXT,
    recorded_at INTEGER,
    FOREIGN KEY(recorded_by) REFERENCES local_peers(peer_id)
);
```

### Pattern 2: Subjective Table (per-peer view)

For events where each peer has their own view:

```sql
CREATE TABLE subjective_table (
    event_id TEXT,
    recorded_by TEXT,
    field1 TEXT,
    recorded_at INTEGER,
    PRIMARY KEY(event_id, recorded_by),
    FOREIGN KEY(recorded_by) REFERENCES local_peers(peer_id)
);
```

### Pattern 3: With Dependencies

For events with parent relationships:

```sql
CREATE TABLE table_with_deps (
    event_id TEXT PRIMARY KEY,
    parent_id TEXT,  -- Track parent
    recorded_by TEXT,
    recorded_at INTEGER,
    FOREIGN KEY(parent_id) REFERENCES other_table(event_id),
    FOREIGN KEY(recorded_by) REFERENCES local_peers(peer_id)
);

-- Separate table for dependency tracking
CREATE TABLE event_dependencies (
    child_event_id TEXT,
    parent_event_id TEXT,
    recorded_by TEXT,
    dependency_type TEXT,  -- 'message', 'group', etc.
    PRIMARY KEY(child_event_id, parent_event_id, recorded_by)
);
```

## Logging Patterns

```python
# Entry point
log.debug(f"create() peer_id={peer_id[:20]}..., parent_id={parent_id[:20]}...")

# Validation failures
log.warning(f"project() validation failed: missing field X")

# Business logic failures
log.warning(f"project() permission denied: peer {peer_id} not in group")

# Success
log.debug(f"project() projected {event_id[:20]}...")
log.info(f"create() created event {event_id[:20]}...")
```

**Rules**:
- Use debug level for routine operations
- Use warning level for validation/permission failures
- Use info level for user-facing operations
- Always truncate IDs with `[:20]...` for readability

## Common Patterns to Avoid

### ❌ DON'T: Extract field without validation

```python
# BAD
field = event_data['field']  # Crashes if missing
```

### ✓ DO: Use .get() with validation

```python
# GOOD
field = event_data.get('field')
if not field:
    log.warning("missing required field")
    return
```

### ❌ DON'T: Duplicate field extraction

```python
# BAD
field1 = event_data.get('field1')
field2 = event_data.get('field2')
# ... 10 more extractions
if not all([field1, field2, ...]):  # Late validation
```

### ✓ DO: Extract all fields upfront with validation

```python
# GOOD
field1 = event_data.get('field1')
field2 = event_data.get('field2')
if not all([field1, field2]):  # Early validation
    return
```

### ❌ DON'T: Recreate database connection

```python
# BAD (in same function)
db.execute(...)
safedb = create_safe_db(db, recorded_by=peer_id)
db.execute(...)  # Wrong! Using unsafe db
```

### ✓ DO: Single database object per function

```python
# GOOD
safedb = create_safe_db(db, recorded_by=peer_id)
safedb.execute(...)  # Consistent throughout
```

## Testing Event Modules

### Unit Test Template

```python
def test_create_event():
    """Test create() function."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Setup: Create prerequisites
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Execute: Create event
    result = event_module.create(
        peer_id=alice['peer_id'],
        field1='value1',
        t_ms=2000,
        db=db
    )
    event_id = result['id']
    db.commit()

    # Verify: Check projection
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    row = safedb.query_one(
        "SELECT field1 FROM table_name WHERE event_id = ? LIMIT 1",
        (event_id,)
    )
    assert row is not None
    assert row['field1'] == 'value1'
```

### Use Test Fixtures (conftest.py)

```python
from tests.conftest import fresh_db_with_alice

def test_create_event(fresh_db_with_alice):
    """Test create() function."""
    db, alice = fresh_db_with_alice

    # Execute: Create event
    result = event_module.create(
        peer_id=alice['peer_id'],
        field1='value1',
        t_ms=2000,
        db=db
    )
    # ... rest of test
```

## Checklist for New Event Modules

- [ ] Module docstring explains domain and purpose
- [ ] `create()` validates inputs upfront
- [ ] `create()` signs event if needed (document which key is used)
- [ ] `create()` encrypts with appropriate key if needed
- [ ] `create()` returns meaningful metadata
- [ ] `project()` uses safedb for access control
- [ ] `project()` validates all fields with .get()
- [ ] `project()` uses INSERT OR IGNORE (idempotent)
- [ ] `project()` records dependencies if applicable
- [ ] `project()` verifies signatures if event is signed
- [ ] `dependencies()` returns complete list
- [ ] Database schema uses standard naming
- [ ] Logging uses debug/warning/info appropriately
- [ ] Tests use fixtures from conftest.py
- [ ] Field names follow conventions

## See Also

- `events/identity/user.py` - Example of complex event module
- `events/content/message.py` - Example of simpler event module
- `events/group/group_key.py` - Example with encryption
