# Device Names Implementation Plan (Updated for Master)

## Goal
Add device names (e.g., "Desktop", "Phone") to distinguish between multiple devices for the same user in the CLI.

## Current State (Post-Master Merge)
- **No more link events** - `link.py` and `link_invite.py` have been deleted
- **peer_shared is the canonical peer event** - represents each device/peer's shareable identity
- **peers_shared table** - stores peer_shared events with user_id linking
- **CLI already supports device_name** - AccountContext.__init__() takes device_name parameter
- **No device names stored yet** - peers_shared table has no device_name column

## Implementation Overview

The peer_shared event is the natural place to store device_name because:
1. Each device/peer has exactly one peer_shared event
2. peer_shared already carries identity information (public_key, user_id, etc.)
3. It's synced across the network (SHAREABLE = True)
4. Projection already stores data in peers_shared table

## Implementation Tasks

### Task 1: Add device_name column to peers_shared table
**File:** `events/identity/peer_shared.sql`

Add column to the peers_shared table:
```sql
ALTER TABLE peers_shared ADD COLUMN device_name TEXT DEFAULT 'Device';
```

### Task 2: Update peer_shared.create() to accept device_name parameter
**File:** `events/identity/peer_shared.py`

Add `device_name` parameter to create():
```python
def create(peer_id: str, t_ms: int, db: Any,
           invite_id: str,
           invite_private_key: bytes,
           device_name: str = "Device") -> str:  # NEW parameter
```

Include device_name in event data:
```python
event_data = {
    'type': 'peer_shared',
    'public_key': crypto.b64encode(public_key),
    'peer_id': peer_id,
    'device_name': device_name,  # NEW
    'created_at': t_ms
}
```

### Task 3: Update peer_shared.project() to store device_name
**File:** `events/identity/peer_shared.py`

Extract device_name from event data and store in peers_shared table:
```python
# In project(), extract device_name from event
device_name = event_data.get('device_name', 'Device')

# When inserting into peers_shared, include the device_name column
safedb.execute(
    """INSERT OR IGNORE INTO peers_shared
       (peer_shared_id, peer_id, public_key, user_id, device_name, created_at, recorded_by, recorded_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
    (
        peer_shared_id,
        event_data['peer_id'],
        event_data['public_key'],
        user_id,
        device_name,  # NEW
        event_data['created_at'],
        recorded_by,
        recorded_at
    )
)
```

### Task 4: Add get_device_name() query function
**File:** `events/identity/peer_shared.py`

Add function to retrieve device name:
```python
def get_device_name(peer_shared_id: str, recorded_by: str, db: Any) -> str:
    """Get device name for a peer_shared_id.

    Args:
        peer_shared_id: The public peer_shared ID
        recorded_by: Peer ID requesting access (for access control)
        db: Database connection

    Returns:
        Device name (e.g., "Phone", "Desktop") or "Device" if not set
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT device_name FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
        (peer_shared_id, recorded_by)
    )
    return row['device_name'] if row and row['device_name'] else "Device"
```

### Task 5: Update peer_shared.join() to pass device_name
**File:** `events/identity/peer_shared.py`

Update join() to accept and forward device_name:
```python
def join(peer_id: str, peer_invite_id: str, peer_invite_private_key: bytes,
         user_id: str | None, prekey_id: str | None, t_ms: int, db: Any,
         device_name: str = "Device") -> dict[str, Any]:  # NEW parameter
```

Pass device_name to create():
```python
peer_shared_id = create(
    peer_id=peer_id,
    t_ms=t_ms,
    db=db,
    invite_id=peer_invite_id,
    invite_private_key=peer_invite_private_key,
    device_name=device_name  # NEW
)
```

### Task 6: Update user.new_network() to accept and pass device_name
**File:** `events/identity/user.py`

Add device_name parameter:
```python
def new_network(name: str, t_ms: int, db: Any, device_name: str = "Device") -> dict[str, Any]:
```

Pass to peer_shared.join():
```python
peer_shared_join_result = peer_shared.join(
    peer_id=peer_id,
    peer_invite_id=peer_invite_id,
    peer_invite_private_key=peer_invite_private_key,
    user_id=user_id,
    prekey_id=bootstrap_invite_prekey_id,
    t_ms=t_ms + 50,
    db=db,
    device_name=device_name  # NEW
)
```

### Task 7: Update user.join() to accept and pass device_name
**File:** `events/identity/user.py`

Add device_name parameter:
```python
def join(peer_id: str, invite_link: str, name: str, t_ms: int, db: Any,
         device_name: str = "Device") -> dict[str, Any]:  # NEW parameter
```

Pass to peer_shared.join():
```python
peer_shared_join_result = peer_shared.join(
    peer_id=peer_id,
    peer_invite_id=peer_invite_id,
    peer_invite_private_key=peer_invite_private_key,
    user_id=user_id,
    prekey_id=invite_prekey_id,
    t_ms=t_ms + 20,
    db=db,
    device_name=device_name  # NEW
)
```

### Task 8: Update CLI to use device_name
**File:** `cli.py`

When creating a network, pass device_name:
```python
# In new_network command
device_name = args.device_name or "Device"
result = user.new_network(
    name=user_name,
    t_ms=session.current_time_ms,
    db=session.db,
    device_name=device_name  # NEW
)
```

When joining, pass device_name:
```python
# In join command
device_name = args.device_name or "Device"
result = user.join(
    peer_id=peer_id,
    invite_link=invite_link,
    name=user_name,
    t_ms=session.current_time_ms,
    db=session.db,
    device_name=device_name  # NEW
)
```

### Task 9: Verify device names in CLI accounts display
**File:** `cli.py`

The display_accounts() function already shows account.full_name which includes device_name:
```python
account.full_name  # Already shows "alice (desktop)" or "alice (phone)"
```

This will automatically display device names once the backend stores them.

### Task 10: Test device linking with CLI
Create a comprehensive test in the CLI:
1. Create a network with device_name="Desktop"
2. Link a second device with device_name="Phone"
3. Verify both devices appear in accounts list with correct names
4. Verify device names are visible from both devices' perspectives
5. Test messaging between devices to ensure full functionality

## Testing Strategy

### Unit tests (update existing tests)
Update scenario tests that use user.new_network() and user.join():
- Pass device_name parameter to calls
- Add assertions to verify device_name is stored correctly

Files to update:
- `tests/scenario_tests/test_multi_device_linking.py`
- Any other tests that use new_network() or join()

### Manual CLI testing
1. Run CLI
2. Create network with device_name="Desktop"
3. Create second device with device_name="Phone"
4. Verify accounts display shows both with correct names
5. Test basic messaging between devices
6. Verify device names persist across restarts

## Files to Modify

### Core Implementation
1. `events/identity/peer_shared.sql` - Add device_name column
2. `events/identity/peer_shared.py` - Add device_name handling in create(), project(), join(), and add get_device_name()
3. `events/identity/user.py` - Add device_name parameter to new_network() and join()
4. `cli.py` - Prompt for device_name and pass through to API calls

### Test Updates
5. `tests/scenario_tests/test_multi_device_linking.py` - Pass device_name to new_network() and verify

## Estimated Effort
- peer_shared.sql change: 2 minutes
- peer_shared.py changes (create, project, join, get_device_name): 20 minutes
- user.py changes (new_network, join): 10 minutes
- cli.py changes: 10 minutes
- Test updates: 15 minutes
- Manual testing: 10 minutes
- **Total: ~1 hour**

## Success Criteria
✓ Device names stored in peer_shared events
✓ Device names displayed in CLI accounts list (format: "alice (desktop)", "alice (phone)")
✓ Device names persist when devices link
✓ Device names visible to other devices via sync
✓ Scenario tests pass with device_name parameter
✓ Manual CLI test: create network, link device, verify both show up with correct names
