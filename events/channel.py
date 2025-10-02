"""Channel event type (shareable, encrypted)."""
from typing import Any
import json
import logging
import crypto
import store
from events import key

log = logging.getLogger(__name__)


def create(name: str, group_id: str, peer_id: str, peer_shared_id: str, key_id: str, t_ms: int, db: Any) -> str:
    """Create a shareable, encrypted channel event in the given group.

    Note: peer_id (local) sees the event; peer_shared_id (public) is the creator identity.
    """
    log.info(f"channel.create() creating channel name='{name}', group_id={group_id}, peer_id={peer_id}")

    # Create event dict
    event_data = {
        'type': 'channel',
        'name': name,
        'group_id': group_id,
        'created_by': peer_shared_id,  # References shareable peer identity
        'created_at': t_ms
    }

    # Get key_data for encryption
    key_data = key.get_key(key_id, db)

    # Wrap (canonicalize + encrypt)
    canonical = crypto.canonicalize_json(event_data)
    blob = crypto.wrap(canonical, key_data, db)

    # Store event with first_seen wrapper and projection
    event_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"channel.create() created channel_id={event_id}")
    return event_id


def project(event_id: str, seen_by_peer_id: str, received_at: int, db: Any) -> None:
    """Project channel event into channels table and shareable_events table."""
    log.debug(f"channel.project() projecting channel_id={event_id}, seen_by={seen_by_peer_id}")

    # Get blob from store
    blob = store.get(event_id, db)
    if not blob:
        log.warning(f"channel.project() blob not found for channel_id={event_id}")
        return

    # Unwrap (decrypt)
    unwrapped, _ = crypto.unwrap(blob, db)
    if not unwrapped:
        log.warning(f"channel.project() unwrap failed for channel_id={event_id}")
        return  # Already blocked by first_seen.project() if keys missing

    # Parse JSON
    event_data = crypto.parse_json(unwrapped)
    log.info(f"channel.project() projected channel name='{event_data.get('name')}', id={event_id}")

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
