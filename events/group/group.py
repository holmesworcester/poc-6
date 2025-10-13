"""Group event type (shareable, encrypted)."""
from typing import Any
import json
import logging
import crypto
import store
from events.group import group_key
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(name: str, peer_id: str, peer_shared_id: str, t_ms: int, db: Any, is_main: bool = False) -> tuple[str, str]:
    """Create a shareable, encrypted group event.

    Groups own their encryption keys. The key is created internally and its id stored
    in the group event for later retrieval.

    Note: peer_id (local) signs and sees the event; peer_shared_id (public) is the creator identity.

    SECURITY: This function trusts that peer_id is correct and owned by the caller.
    In production, the API authentication layer should validate that the authenticated session
    owns this peer_id before calling this function. This is safe for local-only apps where
    the user controls all peers on the device.

    Args:
        is_main: True if this is the peer's main group for inviting (default: False)

    Returns:
        (group_id, key_id): The group event ID and its encryption key ID
    """
    # Create the group's encryption key
    key_id = group_key.create(peer_id=peer_id, t_ms=t_ms, db=db)

    log.info(f"group.create() creating group name='{name}', peer_id={peer_id}, key_id={key_id}, is_main={is_main}")

    # Create event dict
    event_data = {
        'type': 'group',
        'name': name,
        'created_by': peer_shared_id,  # References shareable peer identity
        'created_at': t_ms,
        'key_id': key_id,  # Store key_id in event for later retrieval
        'is_main': 1 if is_main else 0  # Store is_main flag
    }

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Get key_data for encryption
    key_data = group_key.get_key(key_id, peer_id, db)

    # Wrap (canonicalize + encrypt)
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, key_data, db)

    # Store event with recorded wrapper and projection
    event_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group.create() created group_id={event_id}, key_id={key_id}")
    return (event_id, key_id)


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project group event into groups table and shareable_events table."""
    log.debug(f"group.project() projecting group_id={event_id}, seen_by={recorded_by}")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(event_id, unsafedb)
    if not blob:
        log.warning(f"group.project() blob not found for group_id={event_id}")
        return None

    # Unwrap (decrypt)
    unwrapped, _ = crypto.unwrap(blob, recorded_by, db)
    if not unwrapped:
        log.warning(f"group.project() unwrap failed for group_id={event_id}")
        return None  # Already blocked by recorded.project() if keys missing

    # Parse JSON
    event_data = crypto.parse_json(unwrapped)

    # Verify signature - get public key from created_by peer_shared
    from events.identity import peer_shared
    created_by = event_data['created_by']
    public_key = peer_shared.get_public_key(created_by, recorded_by, db)
    if not crypto.verify_event(event_data, public_key):
        log.warning(f"group.project() signature verification FAILED for group_id={event_id}")
        return None  # Reject unsigned or invalid signature

    # Insert into groups table (use REPLACE to overwrite stubs from user.project())
    safedb.execute(
        """INSERT OR REPLACE INTO groups
           (group_id, name, created_by, created_at, key_id, is_main, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            event_data['name'],
            event_data['created_by'],
            event_data['created_at'],
            event_data['key_id'],
            event_data.get('is_main', 0),  # Default to 0 if not present (backward compatibility)
            recorded_by,
            recorded_at
        )
    )

    return event_id


def pick_key(group_id: str, recorded_by: str, db: Any) -> dict[str, Any]:
    """Get the key_data for a group.

    Args:
        group_id: Group ID to get key for
        recorded_by: Peer ID requesting access (for access control)
        db: Database connection

    Returns:
        Key dict for crypto operations

    Raises:
        ValueError: If group not found or peer doesn't have access
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Query groups table for key_id, verifying peer has access
    row = safedb.query_one(
        "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ? LIMIT 1",
        (group_id, recorded_by)
    )
    if not row:
        raise ValueError(f"group not found or access denied: {group_id} for peer {recorded_by}")

    # Get key_data from key (with access control)
    return group_key.get_key(row['key_id'], recorded_by, db)


def list_all_groups(recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List all groups for a specific peer."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return safedb.query(
        "SELECT group_id, name, created_by, created_at FROM groups WHERE recorded_by = ? ORDER BY created_at DESC",
        (recorded_by,)
    )
