"""Group event type (shareable, encrypted)."""
from __future__ import annotations

# Registry metadata
EVENT_TYPE = 'group'
SHAREABLE = True  # Groups sync for access control
PROJECTION_TABLE = ('groups', 'group_id')

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from events.group import group_key
from events.identity import peer
from core.db import create_safe_db, create_unsafe_db
from core.projection_v2.types import ProjectorResult, WriteOp, Command

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

    # Sign the event - use provided key or peer's private key
    if signer_private_key:
        private_key = signer_private_key
    else:
        private_key = peer.get_private_key(peer_id, peer_id, db)

    # Get key_data for encryption
    key_data = group_key.get_key(key_id, peer_id, db)

    blob = wire_format.encode_group_wire_event(
        name=name,
        key_id_b64=key_id,
        is_main=is_main,
        network_id_b64=network_id,
        signed_by_b64=actual_signer_id,
        signer_type=signer_type,
        created_at_ms=t_ms,
        key_data=key_data,
        private_key=private_key,
    )

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

    _wire_shadow_group(name, key_id, is_main, network_id)

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

    # Trigger retry of pending name updates now that group is available
    commands = (
        Command(command_type='retry_pending_name_updates', args={}),
    )

    return ProjectorResult(writes=writes, valid_event=True, commands=commands)


def _wire_shadow_group(name: str, key_id: str, is_main: int, network_id: str | None) -> None:
    """Validate group fields against the fixed-size wire payload layout."""
    plaintext = wire_format.encode_group_plaintext(
        name=name,
        key_id=crypto.b64decode(key_id),
        is_main=is_main,
        network_id=crypto.b64decode(network_id) if network_id else None,
    )
    decoded = wire_format.decode_group_plaintext(plaintext)
    if decoded["key_id"] != crypto.b64decode(key_id):
        raise ValueError("wire shadow decode key_id mismatch")


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


def list(recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List all groups for a specific peer."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return safedb.query(
        "SELECT group_id, name, signed_by, created_at FROM groups WHERE recorded_by = ? ORDER BY created_at DESC",
        (recorded_by,)
    )


def get(group_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get a group by ID.

    Args:
        group_id: Group ID to look up
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        Group dict with group_id, name, key_id, signed_by, etc., or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return safedb.query_one(
        "SELECT group_id, name, key_id, signed_by, created_at FROM groups WHERE group_id = ? AND recorded_by = ?",
        (group_id, recorded_by)
    )


def get_main(recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get the main group for a peer.

    Args:
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        Group dict with group_id, key_id, etc., or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return safedb.query_one(
        "SELECT group_id, name, key_id, signed_by, created_at FROM groups WHERE is_main = 1 AND recorded_by = ? LIMIT 1",
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
