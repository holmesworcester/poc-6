Centralizing All Validation in the Validate Handler
====================================================

Current State
-------------
We have validation logic scattered across multiple handlers:
- `validate.py`: Validates event structure using type-specific validators
- `remove.py`: Checks if events should be dropped (deletions, removals)
- `check_membership.py`: Validates group membership
- Reflectors: Doing their own access checks (e.g., sync_request checking user in network)

Better Approach: Single Validation Point
-----------------------------------------
The `validate` handler should be THE single point for ALL validation:
1. Structural validation (current responsibility)
2. Access control validation
3. Membership validation
4. Removal/deletion checks
5. Any other business rule validation

Benefits
--------
1. **Single Responsibility**: Validate handler truly validates everything
2. **Clear Pipeline Semantics**: Events with `validated=true` have passed ALL checks
3. **Better Error Messages**: Can provide specific validation failure reasons
4. **Easier Testing**: All validation logic in one place
5. **Performance**: Can short-circuit validation on first failure

Proposed Implementation
-----------------------

### Expanded Validate Handler

```python
class ValidateHandler(Handler):
    """
    Comprehensive validation for all events.
    Validates: structure, access control, membership, removal status, and business rules.
    """

    def process(self, envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
        """Run all validation checks in sequence."""

        event_type = envelope.get('event_type') or envelope.get('event_plaintext', {}).get('type')
        if not event_type:
            envelope['error'] = "No event type specified"
            return []

        # 1. Check removal/deletion status
        if not self.check_not_removed(envelope, db):
            envelope['error'] = "Event or related entity has been removed"
            return []  # Drop

        # 2. Check network access
        if not self.check_network_access(envelope, db):
            envelope['error'] = "No access to network"
            return []  # Drop

        # 3. Check group membership (if applicable)
        if not self.check_group_membership(envelope, db):
            envelope['error'] = "Not a member of the group"
            return []  # Drop

        # 4. Check channel access (if applicable)
        if not self.check_channel_access(envelope, db):
            envelope['error'] = "No access to channel"
            return []  # Drop

        # 5. Event-type specific access validation
        if not self.check_event_type_access(envelope, db):
            envelope['error'] = f"Not authorized for {event_type} events"
            return []  # Drop

        # 6. Run structural validation (existing validators)
        validator = self.validators.get(event_type)
        if not validator:
            envelope['error'] = f"No validator for event type: {event_type}"
            return []

        try:
            if not validator.validate(envelope):
                envelope['error'] = "Structural validation failed"
                return []  # Drop
        except Exception as e:
            envelope['error'] = f"Validation error: {str(e)}"
            return []  # Drop

        # All validation passed!
        envelope['validated'] = True
        envelope['access_validated'] = True
        envelope['should_remove'] = False
        return [envelope]
```

### Validation Methods

Add these methods to the ValidateHandler class:

```python
def check_not_removed(self, envelope: dict, db: sqlite3.Connection) -> bool:
    """Check if event or related entities have been removed/deleted."""
    # Check explicit event deletion
    event_id = envelope.get('event_id')
    if event_id:
        cursor = db.execute("SELECT 1 FROM deleted_events WHERE event_id = ?", (event_id,))
        if cursor.fetchone():
            return False

    # Check if author is removed
    peer_id = envelope.get('peer_id')
    if peer_id:
        cursor = db.execute("SELECT 1 FROM removed_users WHERE user_id = ?", (peer_id,))
        if cursor.fetchone():
            return False

    # Check if channel is deleted (for channel events)
    plaintext = envelope.get('event_plaintext', {})
    channel_id = plaintext.get('channel_id')
    if channel_id:
        cursor = db.execute("SELECT 1 FROM deleted_channels WHERE channel_id = ?", (channel_id,))
        if cursor.fetchone():
            return False

    return True

def check_network_access(self, envelope: dict, db: sqlite3.Connection) -> bool:
    """Check if peer has access to the network."""
    plaintext = envelope.get('event_plaintext', {})
    network_id = plaintext.get('network_id') or envelope.get('network_id')
    peer_id = plaintext.get('peer_id') or envelope.get('peer_id')

    if not network_id or not peer_id:
        return True  # Can't check without these

    # Check if peer is a user in the network
    cursor = db.execute(
        "SELECT 1 FROM users WHERE user_id = ? AND network_id = ?",
        (peer_id, network_id)
    )
    return cursor.fetchone() is not None

def check_group_membership(self, envelope: dict, db: sqlite3.Connection) -> bool:
    """Check group membership for group events."""
    plaintext = envelope.get('event_plaintext', {})
    group_id = plaintext.get('group_id')
    if not group_id:
        return True  # Not a group event

    peer_id = plaintext.get('peer_id') or envelope.get('peer_id')
    if not peer_id:
        return False  # Need peer_id for group events

    # Check if peer is a member of the group
    cursor = db.execute(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, peer_id)
    )
    return cursor.fetchone() is not None

def check_event_type_access(self, envelope: dict, db: sqlite3.Connection) -> bool:
    """Event-type specific access checks."""
    event_type = envelope.get('event_type')
    plaintext = envelope.get('event_plaintext', {})

    if event_type == 'sync_request':
        # Check if to_peer is actually our identity in this network
        to_peer = plaintext.get('to_peer')
        network_id = plaintext.get('network_id')
        if to_peer and network_id:
            cursor = db.execute(
                "SELECT 1 FROM users WHERE user_id = ? AND network_id = ?",
                (to_peer, network_id)
            )
            if not cursor.fetchone():
                return False  # Not for us

    # Add other event-type specific checks here

    return True
```

### Simplifying Other Components

With all validation centralized:

1. **Remove Handler**: Can be deleted or reduced to a no-op
2. **Check Membership Handler**: Can be deleted
3. **Reflectors**: Remove all access checks - assume validation passed
4. **Event Validators**: Focus only on structural validation

### Example: Simplified Sync Request Reflector

```python
def sync_request_reflector(envelope: Dict, db: sqlite3.Connection, time_now_ms: int) -> Tuple[bool, List[Dict]]:
    """
    Reflect sync requests with events.
    Assumes validation has already verified we should process this request.
    """
    # No membership checks needed - validate handler already verified:
    # 1. to_peer is our identity in the network
    # 2. from_identity has access to the network
    # 3. The request is valid

    request = envelope.get('event_plaintext', {})
    network_id = request.get('network_id')
    last_sync_ms = request.get('last_sync_ms', 0)

    # Just get and return the events
    events = queries.get_events_since(ReadOnlyConnection(db), {
        'network_id': network_id,
        'last_sync_ms': last_sync_ms,
        'limit': 100
    })

    # Create response envelopes...
    # (rest of reflector logic without access checks)
```

### Pipeline Order

The validate handler should run:
1. After signature verification (needs to trust the author)
2. Before expensive operations (projectors, storage)
3. Before reflectors (they assume validation passed)

### Migration Steps

1. Expand `validate.py` with all validation methods
2. Remove validation logic from reflectors
3. Delete or simplify `remove.py` and `check_membership.py`
4. Update tests to reflect centralized validation
5. Document that `validated=true` means ALL validation passed

### Future Enhancements

1. **Validation Rules Engine**: Define validation rules in configuration
2. **Caching**: Cache validation results for performance
3. **Validation Bypass**: Allow certain trusted sources to skip validation
4. **Validation Metrics**: Track what validations fail most often
5. **Custom Validators**: Allow plugins to add validation rules

Conclusion
----------
Centralizing all validation in the `validate` handler creates cleaner architecture:
- Single source of truth for what's valid
- Clear semantics: `validated=true` means fully validated
- Simpler downstream components that trust validation
- Better maintainability and testing