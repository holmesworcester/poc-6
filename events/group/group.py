"""Group event type (shareable, encrypted)."""

# Registry metadata
EVENT_TYPE = 'group'
SHAREABLE = True  # Groups sync for access control
EPHEMERAL = False
PROJECTION_TABLE = ('groups', 'group_id')

from typing import Any
import json
import logging
from core import crypto
from core import store
from events.group import group_key
from events.identity import peer
from core.db import create_safe_db, create_unsafe_db
from core.projection_v2.types import ProjectorResult, WriteOp

EVENT_SPEC = {
    'encrypted': True,
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {
        'group_key': {
            'source': 'table',
            'table': 'group_keys',
            'key': 'key_id',
            'fields': ['key_id'],
        },
    },
    'optional': {
        'network': {
            'source': 'table',
            'table': 'networks',
            'key': 'network_id',
            'key_from': 'network_id',
            'fields': ['network_id'],
            'required_if_present': True,
        },
    },
    'cascade_on_delete': [],
}

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

    signer_type = 'network' if signer_id and network_id and signer_id == network_id else 'peer_shared'

    # Create event dict
    event_data = {
        'type': 'group',
        'name': name,
        'signed_by': actual_signer_id,  # Network ID or peer_shared_id
        'signer_type': signer_type,
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


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for group events."""
    event_data = ctx.event_data

    if event_data.get('type') != 'group':
        return ProjectorResult(writes=tuple(), valid_event=False)

    name = event_data.get('name')
    signed_by = event_data.get('signed_by')
    key_id = event_data.get('key_id')
    created_at = event_data.get('created_at')

    if not name or not signed_by or not key_id or created_at is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    is_main = event_data.get('is_main', 0)
    network_id = event_data.get('network_id') or ''

    values = {
        'group_id': ctx.event_id,
        'name': name,
        'signed_by': signed_by,
        'created_at': created_at,
        'key_id': key_id,
        'is_main': is_main,
        'network_id': network_id,
        'recorded_at': ctx.recorded_at,
    }

    writes = (
        WriteOp(
            op='insert',
            table='groups',
            values=values,
        ),
        WriteOp(
            op='update',
            table='groups',
            values=values,
            where={
                'group_id': ctx.event_id,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project group event into groups table.

    Supports polymorphic signature verification:
    - If signed_by matches a network_id, verify with network's public key
    - Otherwise, verify with peer_shared's public key
    """
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

    # Polymorphic signature verification
    # Check if signed_by is a network_id or a peer_shared_id
    # Use store-based lookups to avoid timing issues with projection tables
    signed_by = event_data['signed_by']

    # Try network first (from store - avoids timing issues with projection tables)
    from events.identity import network
    public_key = network.get_public_key_from_store(signed_by, db)

    if public_key:
        log.debug(f"group.project() verifying network-signed group {event_id[:20]}...")
    else:
        # Try peer_shared (from store)
        from events.identity import peer_shared
        public_key = peer_shared.get_public_key_from_store(signed_by, db)
        if public_key:
            log.debug(f"group.project() verifying peer-signed group {event_id[:20]}...")
        else:
            log.warning(f"group.project() signed_by={signed_by[:20]}... not found as network or peer_shared")
            return None

    if not crypto.verify_event(event_data, public_key):
        log.warning(f"group.project() signature verification FAILED for group_id={event_id}")
        return None  # Reject unsigned or invalid signature

    # Extract network_id from event data (for dependency ordering)
    network_id = event_data.get('network_id', '')

    # Insert into groups table (use REPLACE to overwrite stubs from user.project())
    safedb.execute(
        """INSERT OR REPLACE INTO groups
           (group_id, name, signed_by, created_at, key_id, is_main, network_id, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            event_data['name'],
            event_data['signed_by'],
            event_data['created_at'],
            event_data['key_id'],
            event_data.get('is_main', 0),  # Default to 0 (not main group)
            network_id,
            recorded_by,
            recorded_at
        )
    )

    if network_id:
        log.info(f"group.project() stored group {event_id[:20]}... in network {network_id[:20]}...")

    # DETERMINISTIC TRIGGER: Retry pending name updates now that group is available
    # This handles the case where group_key_shared arrived before the group event
    from events.group.group_key_shared import retry_pending_name_updates
    retry_pending_name_updates(recorded_by, db)

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


def get_current_key(group_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get the current key ID for a group.

    Args:
        group_id: Group ID
        recorded_by: Peer ID requesting access
        db: Database connection

    Returns:
        Dict with key_id, or None if group not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ? LIMIT 1",
        (group_id, recorded_by)
    )
    return row
