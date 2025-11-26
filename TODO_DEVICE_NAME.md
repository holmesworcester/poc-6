# TODO: Add device_name to link event

## Background

The CLI design uses device names to distinguish between multiple devices for the same user:
- `Alice (Desktop)` - Alice's desktop device
- `Alice (Phone)` - Alice's phone device

Both devices share the same `user_id` but have different `peer_id`s and `device_name`s.

## Current State

Currently, device names are not stored in the system. When a device links to an existing user via `link.join()`, there's no way to specify or retrieve a device name.

## Required Changes

### 1. Add device_name to link event schema

**File**: `events/identity/link.sql` (or wherever link events are defined)

Add `device_name` field to the link event:
```sql
-- In the link event payload
device_name TEXT  -- e.g., "Desktop", "Phone", "Laptop"
```

### 2. Update link.join() function

**File**: `events/identity/link.py`

Add `device_name` parameter to `join()` function:
```python
def join(link_url, device_name, t_ms, db):
    """
    Join a network by accepting a link invite.

    Args:
        link_url: The link invite URL
        device_name: Name for this device (e.g., "Desktop", "Phone")
        t_ms: Timestamp
        db: Database connection

    Returns:
        Dictionary with peer_id, user_id, etc.
    """
    # ... existing code ...

    # Include device_name in the link event
    link_data = {
        'type': 'link',
        'device_name': device_name,  # NEW
        # ... other fields ...
    }
```

### 3. Update link event projection

**File**: Wherever link events are projected (likely in `events/identity/link.py` or `projection.py`)

Store device_name in a queryable table:
```sql
CREATE TABLE IF NOT EXISTS device_names (
    peer_id TEXT PRIMARY KEY,
    device_name TEXT NOT NULL,
    recorded_by TEXT NOT NULL
);
```

Or add to existing peer/user tables if more appropriate.

### 4. Add query function

**File**: `events/identity/link.py` or `events/identity/peer.py`

Add function to retrieve device name:
```python
def get_device_name(peer_id, recorded_by, db):
    """Get the device name for a peer."""
    from db import create_safe_db
    safedb = create_safe_db(db, recorded_by=recorded_by)

    result = safedb.query_one(
        "SELECT device_name FROM device_names WHERE peer_id = ? AND recorded_by = ?",
        (peer_id, recorded_by)
    )

    return result['device_name'] if result else "Unknown"
```

### 5. Update new_network() for consistency

**File**: `events/identity/user.py`

The `new_network()` function should also support device_name:
```python
def new_network(name, device_name, t_ms, db):
    """
    Create a new network with the first user.

    Args:
        name: User's display name
        device_name: Name for this device (e.g., "Desktop")
        t_ms: Timestamp
        db: Database connection
    """
    # Store device_name as part of peer creation or in a separate event
```

### 6. Update CLI to use device names

**File**: `cli.py` (when implemented)

CLI commands should pass device_name:
```python
# Create network
result = user.new_network(
    name='Alice',
    device_name='Desktop',
    t_ms=session.current_time_ms,
    db=session.db
)

# Link device
result = link.join(
    link_url=link_url,
    device_name='Phone',
    t_ms=session.current_time_ms,
    db=session.db
)
```

## Design Questions

1. **Where to store device_name?**
   - Option A: In link event (most natural)
   - Option B: Separate device_name event
   - Option C: As part of peer creation

   **Recommendation**: Link event (Option A) for linked devices, but also need to handle initial device in `new_network()`

2. **Can device_name be changed?**
   - Probably not needed for MVP
   - If needed later, create a `device_rename` event

3. **Should device_name be unique per user?**
   - No enforcement needed - users can have two "Desktop" devices if they want
   - CLI should show all devices regardless of name collision

4. **Default device name?**
   - If not specified, default to "Device 1", "Device 2", etc.
   - Or require explicit device_name (better UX)

## Testing

Add tests for:
1. Creating network with device name
2. Linking device with device name
3. Querying device names from different peers
4. Multiple devices with same device name (should work)
5. Device name visible after sync

Example test:
```python
def test_device_names():
    # Alice creates network on Desktop
    alice_desktop = user.new_network(name='Alice', device_name='Desktop', t_ms=1000, db=db)

    # Alice links phone
    link_invite_id, link_url, _ = link_invite.create(peer_id=alice_desktop['peer_id'], t_ms=2000, db=db)
    alice_phone = link.join(link_url=link_url, device_name='Phone', t_ms=3000, db=db)

    # Verify same user_id
    assert alice_desktop['user_id'] == alice_phone['user_id']

    # Verify different device names
    desktop_name = link.get_device_name(alice_desktop['peer_id'], alice_desktop['peer_id'], db)
    phone_name = link.get_device_name(alice_phone['peer_id'], alice_phone['peer_id'], db)

    assert desktop_name == 'Desktop'
    assert phone_name == 'Phone'
```

## Implementation Priority

**Priority**: Medium-High

This is needed for the CLI to work properly with multi-device scenarios. Without it, the CLI can't distinguish between devices in a user-friendly way.

## Related Files

- `events/identity/link.py` - Link event creation and projection
- `events/identity/user.py` - Network creation
- `events/identity/peer.py` - Peer creation (may need device_name here too)
- `tests/scenario_tests/test_link_device_*.py` - Tests to update

## Estimated Effort

- Schema changes: 30 minutes
- Function updates: 1 hour
- Query function: 30 minutes
- Tests: 1 hour
- **Total: ~3 hours**
