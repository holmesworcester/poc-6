"""Channel event type (shareable, encrypted)."""
from typing import Any
import json
import logging
import crypto
import store
from events.group import group_key
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(name: str, group_id: str, peer_id: str, peer_shared_id: str, key_id: str, t_ms: int, db: Any, is_main: bool = False) -> str:
    """Create a shareable, encrypted channel event in the given group.

    Note: peer_id (local) sees the event; peer_shared_id (public) is the creator identity.

    SECURITY: This function trusts that peer_id is correct and owned by the caller.
    In production, the API authentication layer should validate that the authenticated session
    owns this peer_id before calling this function. This is safe for local-only apps where
    the user controls all peers on the device.

    Args:
        is_main: True if this is the peer's main channel (default: False)
    """
    log.info(f"channel.create() creating channel name='{name}', group_id={group_id}, peer_id={peer_id}, is_main={is_main}")

    # Create event dict
    event_data = {
        'type': 'channel',
        'name': name,
        'group_id': group_id,
        'created_by': peer_shared_id,  # References shareable peer identity
        'created_at': t_ms,
        'is_main': 1 if is_main else 0  # Store is_main flag
    }

    # Get key_data for encryption
    key_data = group_key.get_key(key_id, peer_id, db)

    # Wrap (canonicalize + encrypt)
    canonical = crypto.canonicalize_json(event_data)
    blob = crypto.wrap(canonical, key_data, db)

    # Store event with recorded wrapper and projection
    event_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"channel.create() created channel_id={event_id}")
    return event_id


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Project channel event into channels table and shareable_events table."""
    log.warning(f"[CHANNEL_PROJECT_START] channel.project() CALLED for channel_id={event_id[:20]}... recorded_by={recorded_by[:20]}... recorded_at={recorded_at}")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(event_id, unsafedb)
    if not blob:
        log.warning(f"channel.project() blob not found for channel_id={event_id}")
        log.warning(f"[ASSERT] channel.project() blob must exist before projection - FAILING!")
        raise RuntimeError(f"[CHANNEL_PROJECT_ERROR] blob not found for channel_id={event_id}")

    # Unwrap (decrypt)
    unwrapped, _ = crypto.unwrap(blob, recorded_by, db)
    if not unwrapped:
        log.warning(f"channel.project() unwrap failed for channel_id={event_id}")
        log.warning(f"[ASSERT] channel.project() unwrap must succeed before projection - FAILING!")
        raise RuntimeError(f"[CHANNEL_PROJECT_ERROR] unwrap failed for channel_id={event_id}, recorded_by={recorded_by[:20]}...")

    # Parse JSON
    event_data = crypto.parse_json(unwrapped)
    log.warning(f"[CHANNEL_PROJECT] ✓ channel_id={event_id[:20]}... name='{event_data.get('name')}' recorded_by={recorded_by[:20]}...")

    # Check: Verify all required fields exist
    assert event_data.get('name'), f"[CHANNEL_ASSERT] channel must have name"
    assert event_data.get('group_id'), f"[CHANNEL_ASSERT] channel must have group_id"
    assert event_data.get('created_by'), f"[CHANNEL_ASSERT] channel must have created_by"
    assert isinstance(event_data.get('created_at'), int), f"[CHANNEL_ASSERT] channel must have created_at as int, got {type(event_data.get('created_at'))}"
    log.warning(f"[CHANNEL_ASSERT] ✓ All required fields present for channel {event_id[:20]}...")

    # Insert into channels table (use REPLACE to overwrite stubs from user.project())
    log.warning(f"[CHANNEL_PROJECT] Inserting into channels table: channel_id={event_id[:20]}... recorded_by={recorded_by[:20]}... recorded_at={recorded_at}")

    # Before insert, check current state
    existing = safedb.query(
        "SELECT * FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (event_id, recorded_by)
    )
    if existing:
        log.warning(f"[CHANNEL_INSERT_REPLACE] Already exists: {len(existing)} rows for channel_id={event_id[:20]}... recorded_by={recorded_by[:20]}...")

    safedb.execute(
        """INSERT OR REPLACE INTO channels
           (channel_id, name, group_id, created_by, created_at, is_main, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            event_data['name'],
            event_data['group_id'],
            event_data['created_by'],
            event_data['created_at'],
            event_data.get('is_main', 0),  # Default to 0 if not present (backward compatibility)
            recorded_by,
            recorded_at
        )
    )

    # After insert, verify it was actually inserted
    after_insert = safedb.query(
        "SELECT * FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (event_id, recorded_by)
    )
    if after_insert:
        log.warning(f"[CHANNEL_PROJECT_INSERTED] ✓ Row inserted for channel_id={event_id[:20]}... recorded_by={recorded_by[:20]}... recorded_at={recorded_at} ({len(after_insert)} total rows)")
    else:
        log.warning(f"[CHANNEL_INSERT_FAILED] ✗ Row NOT found after insert! channel_id={event_id[:20]}... recorded_by={recorded_by[:20]}...")
        # This is a major bug - the insert failed silently
        assert False, f"[CHANNEL_ASSERT] Insert failed for channel {event_id[:20]}... recorded_by={recorded_by[:20]}..."


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
