"""Group event type (shareable, signed).

Note: In the sender key model, groups no longer have a group_key. Each sender
has their own key (secret) for their messages. Group events are signed but
not encrypted - group membership metadata is public.
"""
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
from events.identity import peer
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp, Command
from core.projection.apply import register_command_handler

EVENT_SPEC = {
    'encrypted': False,  # Sender key model: group metadata is public, messages use sender keys
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {},  # No group_key dependency in sender key model
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
           signer_id: str | None = None, signer_private_key: bytes | None = None) -> tuple[str, str | None]:
    """Create a shareable, signed group event.

    In the sender key model, groups are unencrypted - group membership metadata is public.
    Messages within the group use sender keys for encryption.

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
        (group_id, None): The group event ID and None (no key_id in sender key model)
    """
    # Determine signer - either explicit (network) or default (peer_shared)
    actual_signer_id = signer_id if signer_id else peer_shared_id

    log.info(f"group.create() creating group name='{name}', peer_id={peer_id}, is_main={is_main}, signed_by={actual_signer_id}")

    signer_type = 'network' if signer_id and network_id and signer_id == network_id else 'peer_shared'

    # Sign the event - use provided key or peer's private key
    if signer_private_key:
        private_key = signer_private_key
    else:
        private_key = peer.get_private_key(peer_id, peer_id, db)

    blob = wire_format.encode_group_wire_event(
        name=name,
        is_main=is_main,
        network_id_b64=network_id,
        signed_by_b64=actual_signer_id,
        signer_type=signer_type,
        created_at_ms=t_ms,
        private_key=private_key,
    )

    # Store event with recorded wrapper and projection
    event_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group.create() created group_id={event_id}")
    return (event_id, None)


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for group events."""
    event_data = ctx.event_data

    if event_data.get('type') != 'group':
        return ProjectorResult(writes=tuple(), valid_event=False)

    name = event_data.get('name')
    signed_by = event_data.get('signed_by')
    created_at = event_data.get('created_at')

    # In sender key model, key_id is not required (will be zero-encoded placeholder)
    if not name or not signed_by or created_at is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    is_main = event_data.get('is_main', 0)
    network_id = event_data.get('network_id') or ''

    _wire_shadow_group(name, is_main, network_id)

    values = {
        'group_id': ctx.event_id,
        'name': name,
        'signed_by': signed_by,
        'created_at': created_at,
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


def _wire_shadow_group(name: str, is_main: int, network_id: str | None) -> None:
    """Validate group fields against the fixed-size wire payload layout."""
    # In sender key model, key_id is a zero placeholder
    key_id_bytes = b"\x00" * 16
    plaintext = wire_format.encode_group_plaintext(
        name=name,
        key_id=key_id_bytes,
        is_main=is_main,
        network_id=crypto.b64decode(network_id) if network_id else None,
    )
    # Validate round-trip encoding
    wire_format.decode_group_plaintext(plaintext)


def verify_access(group_id: str, recorded_by: str, db: Any) -> bool:
    """Verify a peer has access to a group.

    In sender key model, groups don't have encryption keys.
    Messages use sender keys for encryption.

    Args:
        group_id: Group ID to check access for
        recorded_by: Peer ID requesting access

    Returns:
        True if peer has access, False otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    row = safedb.query_one(
        "SELECT group_id FROM groups WHERE group_id = ? AND recorded_by = ? LIMIT 1",
        (group_id, recorded_by)
    )
    return row is not None


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
        Group dict with group_id, name, signed_by, etc., or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return safedb.query_one(
        "SELECT group_id, name, signed_by, created_at FROM groups WHERE group_id = ? AND recorded_by = ?",
        (group_id, recorded_by)
    )


def get_main(recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get the main group for a peer.

    Args:
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        Group dict with group_id, name, signed_by, etc., or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return safedb.query_one(
        "SELECT group_id, name, signed_by, created_at FROM groups WHERE is_main = 1 AND recorded_by = ? LIMIT 1",
        (recorded_by,)
    )


def _handle_retry_pending_name_updates(args: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Retry creating name update events that were waiting for key/group availability.

    Called when a group is projected, which may make the main group available
    for pending name updates that couldn't be created during join.
    """
    from events.identity import username_update, peer_name_update, network_name_update

    # Unwrap db if it's already a SafeDB or UnsafeDB wrapper
    raw_db = db._db if hasattr(db, '_db') else db
    safedb = create_safe_db(raw_db, recorded_by=recorded_by)

    # Find all pending items waiting for key
    # Note: safedb.query() already returns a list
    pending_items = safedb.query(
        """SELECT id, type, entity_id, name, peer_id, peer_shared_id, created_at
           FROM pending_name_updates
           WHERE status = 'waiting_for_key' AND recorded_by = ?""",
        (recorded_by,)
    )

    if not pending_items:
        return

    log.info(f"retry_pending_name_updates: found {len(pending_items)} pending items")

    for item in pending_items:
        item_id = item['id']
        item_type = item['type']
        entity_id = item['entity_id']
        name = item['name']
        peer_id = item['peer_id']
        peer_shared_id = item['peer_shared_id']
        created_at = item['created_at']

        try:
            if item_type == 'username':
                username_update.create(
                    user_id=entity_id,
                    name=name,
                    peer_id=peer_id,
                    peer_shared_id=peer_shared_id,
                    t_ms=created_at,
                    db=raw_db,
                )
            elif item_type == 'peer_name':
                peer_name_update.create(
                    peer_target_id=entity_id,
                    name=name,
                    peer_id=peer_id,
                    peer_shared_id=peer_shared_id,
                    t_ms=created_at,
                    db=raw_db,
                )
            elif item_type == 'network_name':
                network_name_update.create(
                    network_id=entity_id,
                    name=name,
                    peer_id=peer_id,
                    peer_shared_id=peer_shared_id,
                    t_ms=created_at,
                    db=raw_db,
                )
            else:
                log.warning(f"retry_pending_name_updates: unknown type '{item_type}' for {item_id}")
                continue

            # Mark as created
            safedb.execute(
                "UPDATE pending_name_updates SET status = 'created' WHERE id = ? AND recorded_by = ?",
                (item_id, recorded_by)
            )
            log.info(f"retry_pending_name_updates: created {item_type} for entity {entity_id[:20]}...")

        except username_update.KeyNotAvailableError:
            # Still not ready, keep waiting
            log.debug(f"retry_pending_name_updates: still waiting for key for {item_type} {entity_id[:20]}...")
        except peer_name_update.KeyNotAvailableError:
            log.debug(f"retry_pending_name_updates: still waiting for key for {item_type} {entity_id[:20]}...")
        except network_name_update.KeyNotAvailableError:
            log.debug(f"retry_pending_name_updates: still waiting for key for {item_type} {entity_id[:20]}...")
        except Exception as e:
            # Mark as failed with error
            safedb.execute(
                "UPDATE pending_name_updates SET status = 'failed', error = ? WHERE id = ? AND recorded_by = ?",
                (str(e), item_id, recorded_by)
            )
            log.warning(f"retry_pending_name_updates: failed to create {item_type} for {entity_id[:20]}...: {e}")


# Register the command handler at module load time
register_command_handler('retry_pending_name_updates', _handle_retry_pending_name_updates)
