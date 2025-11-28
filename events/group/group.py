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


def create(name: str, peer_id: str, peer_shared_id: str, t_ms: int, db: Any,
           is_main: bool = False, network_id: str | None = None,
           signer_id: str | None = None, signer_private_key: bytes | None = None) -> tuple[str, str]:
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
        network_id: Network ID this group belongs to (for dependency ordering)
        signer_id: Optional signer ID (e.g., network_id for network-signed all_users group)
                   If not provided, uses peer_shared_id
        signer_private_key: Optional private key for signing (required if signer_id provided)
                            If not provided, uses peer's private key

    Returns:
        (group_id, key_id): The group event ID and its encryption key ID
    """
    # Create the group's encryption key
    key_id = group_key.create(peer_id=peer_id, t_ms=t_ms, db=db)

    # Determine signer - either explicit (network) or default (peer_shared)
    actual_signer_id = signer_id if signer_id else peer_shared_id

    log.info(f"group.create() creating group name='{name}', peer_id={peer_id}, key_id={key_id}, is_main={is_main}, signed_by={actual_signer_id}")

    # Create event dict
    event_data = {
        'type': 'group',
        'name': name,
        'signed_by': actual_signer_id,  # Network ID or peer_shared_id
        'created_at': t_ms,
        'key_id': key_id,  # Store key_id in event for later retrieval
        'is_main': 1 if is_main else 0  # Store is_main flag
    }

    # Add network_id for dependency ordering (ensures network projects before group)
    if network_id:
        event_data['network_id'] = network_id

    # Sign the event - use provided key or peer's private key
    if signer_private_key:
        private_key = signer_private_key
    else:
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
    """Project group event into groups table.

    Uses pure functional projector from projectors.group.
    Note: Uses INSERT OR REPLACE (not IGNORE) to overwrite stubs from user.project().
    """
    log.debug(f"group.project() projecting group_id={event_id[:20]}...")

    from projectors import resolve
    from projectors import group as group_projector

    safedb = create_safe_db(db, recorded_by=recorded_by)

    input_dict = resolve("group", event_id, recorded_by, recorded_at, db)
    if not input_dict:
        return None

    result = group_projector.project(input_dict)

    if result.blocked or not result.valid:
        log.warning(f"group.project() failed: {result.reason}")
        return None

    # Apply with INSERT OR REPLACE (special case - overwrites stubs)
    for row in result.tables.get("groups", []):
        safedb.execute(
            """INSERT OR REPLACE INTO groups
               (group_id, name, signed_by, created_at, key_id, is_main, network_id, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["group_id"],
                row["name"],
                row["signed_by"],
                row["created_at"],
                row["key_id"],
                row["is_main"],
                row["network_id"],
                row["recorded_by"],
                row["recorded_at"]
            )
        )

    log.info(f"group.project() stored group {event_id[:20]}...")
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
        "SELECT group_id, name, signed_by, created_at FROM groups WHERE recorded_by = ? ORDER BY created_at DESC",
        (recorded_by,)
    )
