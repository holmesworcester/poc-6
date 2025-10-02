"""Channel event type (shareable, encrypted)."""
from typing import Any
import json
import crypto
import store
from events import key


def create(name: str, group_id: str, created_by_peer_id: str, key_id: str, t_ms: int, db: Any) -> str:
    """Create a channel event (shareable, encrypted), store with first_seen, return event_id."""
    # Create event dict
    event_data = {
        'type': 'channel',
        'name': name,
        'group_id': group_id,
        'created_by': created_by_peer_id,
        'created_at': t_ms
    }

    # Get key_data for encryption
    key_data = key.get_key(key_id, db)

    # Wrap (canonicalize + encrypt)
    blob = crypto.wrap(event_data, key_data, db)

    # Store with first_seen (shareable event)
    event_id = store.store_with_first_seen(blob, created_by_peer_id, t_ms, db)

    return event_id


def project(event_id: str, seen_by_peer_id: str, received_at: int, db: Any) -> None:
    """Project channel event into channels table and shareable_events table."""
    # Get blob from store
    blob = store.get(event_id, db)
    if not blob:
        return

    # Unwrap (decrypt)
    unwrapped = crypto.unwrap(blob, db)
    if not unwrapped:
        return

    # Parse JSON
    event_data = json.loads(unwrapped.decode() if isinstance(unwrapped, bytes) else unwrapped)

    # Insert into channels table
    db.execute(
        """INSERT OR IGNORE INTO channels
           (channel_id, name, group_id, created_by, created_at, seen_by_peer_id, received_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            event_data['name'],
            event_data['group_id'],
            event_data['created_by'],
            event_data['created_at'],
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


def list_channels(seen_by_peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all channels for a specific peer."""
    return db.query(
        """SELECT channel_id, name, group_id, created_by, created_at
           FROM channels
           WHERE seen_by_peer_id = ?
           ORDER BY created_at DESC""",
        (seen_by_peer_id,)
    )
