"""Channel event type (shareable, encrypted)."""
from typing import Any
import json
import logging
import crypto
import store
from events import key
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(name: str, group_id: str, peer_id: str, peer_shared_id: str, key_id: str, t_ms: int, db: Any) -> str:
    """Create a shareable, encrypted channel event in the given group.

    Note: peer_id (local) sees the event; peer_shared_id (public) is the creator identity.

    SECURITY: This function trusts that peer_id is correct and owned by the caller.
    In production, the API authentication layer should validate that the authenticated session
    owns this peer_id before calling this function. This is safe for local-only apps where
    the user controls all peers on the device.
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
    key_data = key.get_key(key_id, peer_id, db)

    # Wrap (canonicalize + encrypt)
    canonical = crypto.canonicalize_json(event_data)
    blob = crypto.wrap(canonical, key_data, db)

    # Store event with recorded wrapper and projection
    event_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"channel.create() created channel_id={event_id}")
    return event_id


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Project channel event into channels table and shareable_events table."""
    log.debug(f"channel.project() projecting channel_id={event_id}, seen_by={recorded_by}")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(event_id, unsafedb)
    if not blob:
        log.warning(f"channel.project() blob not found for channel_id={event_id}")
        return

    # Unwrap (decrypt)
    unwrapped, _ = crypto.unwrap(blob, recorded_by, db)
    if not unwrapped:
        log.warning(f"channel.project() unwrap failed for channel_id={event_id}")
        return  # Already blocked by recorded.project() if keys missing

    # Parse JSON
    event_data = crypto.parse_json(unwrapped)
    log.info(f"channel.project() projected channel name='{event_data.get('name')}', id={event_id}")

    # Insert into channels table (use REPLACE to overwrite stubs from user.project())
    safedb.execute(
        """INSERT OR REPLACE INTO channels
           (channel_id, name, group_id, created_by, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            event_data['name'],
            event_data['group_id'],
            event_data['created_by'],
            event_data['created_at'],
            recorded_by,
            recorded_at
        )
    )


def list_channels(recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List all channels for a specific peer."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return safedb.query(
        """SELECT channel_id, name, group_id, created_by, created_at
           FROM channels
           WHERE recorded_by = ?
           ORDER BY created_at DESC""",
        (recorded_by,)
    )
