"""Group event type (shareable, encrypted)."""
from typing import Any
import json
import crypto
import store
from events import key, peer


def create(name: str, created_by_peer_id: str, key_id: str, t_ms: int, db: Any) -> str:
    """Create a group event (shareable, encrypted), store with first_seen, return event_id."""
    # Create event dict
    event_data = {
        'type': 'group',
        'name': name,
        'created_by': created_by_peer_id,
        'created_at': t_ms,
        'key_id': key_id  # Store key_id in event for later retrieval
    }

    # Sign the event
    private_key = peer.get_private_key(created_by_peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Get key_data for encryption
    key_data = key.get_key(key_id, db)

    # Wrap (canonicalize + encrypt)
    blob = crypto.wrap(signed_event, key_data, db)

    # Store with first_seen (shareable event)
    event_id = store.store_with_first_seen(blob, created_by_peer_id, t_ms, db)

    return event_id


def project(event_id: str, seen_by_peer_id: str, received_at: int, db: Any) -> str | None:
    """Project group event into groups table and shareable_events table."""
    # Get blob from store
    blob = store.get(event_id, db)
    if not blob:
        return None

    # Unwrap (decrypt)
    unwrapped = crypto.unwrap(blob, db)
    if not unwrapped:
        return None

    # Parse JSON
    event_data = json.loads(unwrapped.decode() if isinstance(unwrapped, bytes) else unwrapped)

    # Verify signature - get public key from created_by peer
    created_by = event_data['created_by']
    public_key = peer.get_public_key(created_by, db)
    if not crypto.verify_event(event_data, public_key):
        return None  # Reject unsigned or invalid signature

    # Insert into groups table
    db.execute(
        """INSERT OR IGNORE INTO groups
           (group_id, name, created_by, created_at, key_id, seen_by_peer_id, received_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            event_data['name'],
            event_data['created_by'],
            event_data['created_at'],
            event_data['key_id'],
            seen_by_peer_id,
            received_at
        )
    )

    # Insert into shareable_events
    db.execute(
        """INSERT OR IGNORE INTO shareable_events (event_id, peer_id, created_at)
           VALUES (?, ?, ?)""",
        (
            event_id,
            event_data['created_by'],
            event_data['created_at']
        )
    )

    return event_id


def pick_key(group_id: str, db: Any) -> dict[str, Any]:
    """Get the key_data for a group."""
    # Query groups table for key_id
    row = db.query_one("SELECT key_id FROM groups WHERE group_id = ?", (group_id,))
    if not row:
        raise ValueError(f"group not found: {group_id}")

    # Get key_data from key
    return key.get_key(row['key_id'], db)


def list_all_groups(seen_by_peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all groups for a specific peer."""
    return db.query(
        "SELECT group_id, name, created_by, created_at FROM groups WHERE seen_by_peer_id = ? ORDER BY created_at DESC",
        (seen_by_peer_id,)
    )
