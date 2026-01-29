"""Peer removed event type - marks a peer as removed from syncing."""

# Registry metadata
EVENT_TYPE = 'peer_removed'
SHAREABLE = True  # Peer removal syncs to stop syncing with removed peer
PROJECTION_TABLE = None

# Wire format constants
WIRE_TYPE_CODE = 0x26  # TYPE_PEER_REMOVED
WIRE_PLAINTEXT_SIZE = 344  # PEER_REMOVED_PLAINTEXT_SIZE

from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db, create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp, Command
from core.projection.apply import register_command_handler

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for peer_removed event type

def encode_plaintext(removed_peer_id: bytes) -> bytes:
    """Encode a peer_removed payload plaintext.

    Layout (344 bytes):
    - removed_peer_id (16)
    - pad (remaining bytes)
    """
    wire_format._require_len("removed_peer_id", removed_peer_id, 16)
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = removed_peer_id
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a peer_removed payload plaintext."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"peer_removed plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {"removed_peer_id": data[0:16]}


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a peer_removed wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    removed_peer_id_b64: str,
    removed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    """Encode a complete peer_removed wire event."""
    removed_peer_id = crypto.b64decode(removed_peer_id_b64)
    signer_id = crypto.b64decode(removed_by_b64)
    plaintext = encode_plaintext(removed_peer_id=removed_peer_id)
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=0,
        signer_type=wire_format.signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=wire_format._require_len("signer_id", signer_id, wire_format.SIGNER_ID_SIZE),
    )
    signed_bytes = wire_format._signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = wire_format._pad_payload(plaintext)
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a peer_removed wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for peer_removed")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    event_data = {
        "type": EVENT_TYPE,
        "removed_peer_shared_id": crypto.b64encode(decoded["removed_peer_id"]),
        "removed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }
    return event_data


# V2 Projector specification
# Note: peer_removed uses 'removed_by' as the signer field (not standard 'signed_by')
EVENT_SPEC = {
    'encrypted': False,
    'signer': {
        'id_field': 'removed_by',  # Custom signer field
        'type_field': 'signer_type',  # Always 'peer_shared'
    },
    'requires': {
        # The signer's peer_shared must exist for signature verification
        'signer_peer_shared': {
            'source': 'table',
            'table': 'peers_shared',
            'key': 'peer_shared_id',
            'key_from': 'removed_by',
            'fields': ['peer_shared_id', 'public_key'],
        },
    },
    'optional': {
        # The removed peer's info (for user lookup during key rotation)
        'removed_peer': {
            'source': 'table',
            'table': 'peers_shared',
            'key': 'peer_shared_id',
            'key_from': 'removed_peer_shared_id',
            'fields': ['peer_shared_id', 'user_id'],
        },
    },
}


def validate(removed_peer_shared_id: str, removed_by_peer_shared_id: str, recorded_by: str, db: Any) -> bool:
    """Validate that removed_by has authorization to remove the peer.

    Authorization rule:
    - Only admins can remove peers

    This prevents non-admin peers from rotating group keys and excluding other members.

    Args:
        removed_peer_shared_id: peer_shared_id being removed
        removed_by_peer_shared_id: peer_shared_id removing the peer
        recorded_by: Local peer ID for database lookups
        db: Database connection

    Returns:
        True if authorized (is admin), False otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Check if removed_by is an admin
    # Use centralized is_admin() function which handles both normal admins and first_peer
    from events.identity import invite
    return invite.is_admin(removed_by_peer_shared_id, recorded_by, db)


def create(removed_peer_shared_id: str, removed_by_peer_shared_id: str, removed_by_local_peer_id: str, t_ms: int, db: Any) -> str:
    """Create a peer_removed event.

    Args:
        removed_peer_shared_id: peer_shared_id to remove
        removed_by_peer_shared_id: peer_shared_id of remover
        removed_by_local_peer_id: Local peer ID of remover (for signing)
        t_ms: Timestamp
        db: Database connection

    Returns:
        event_id of the created peer_removed event
    """
    # Validate authorization
    if not validate(removed_peer_shared_id, removed_by_peer_shared_id, removed_by_local_peer_id, db):
        raise ValueError("Not authorized to remove this peer")

    _wire_shadow_peer_removed(removed_peer_shared_id)

    from events.identity import peer
    private_key = peer.get_private_key(removed_by_local_peer_id, removed_by_local_peer_id, db)
    blob = encode_wire_event(
        removed_peer_id_b64=removed_peer_shared_id,
        removed_by_b64=removed_by_peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=private_key,
    )
    event_id = store.event(blob, removed_by_local_peer_id, t_ms, db)

    return event_id


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for peer_removed - returns writes without side effects.

    Core projection: insert into removed_peers table.
    Side effects (connection removal, key rotation) are handled in project() wrapper.
    """
    event_data = ctx.event_data

    removed_peer_shared_id = event_data.get('removed_peer_shared_id')
    removed_at = event_data.get('created_at')
    signed_by = event_data.get('removed_by')
    signer = ctx.signer or {}
    signer_user_id = signer.get('user_id')
    signer_is_admin = signer.get('is_admin')

    if not removed_peer_shared_id:
        log.warning(f"peer_removed.project_pure() missing removed_peer_shared_id")
        return ProjectorResult(writes=tuple(), valid_event=False)

    if not signed_by:
        log.warning(f"peer_removed.project_pure() missing removed_by field")
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_peer_removed(removed_peer_shared_id)

    if not signer_user_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    removed_peer_row = ctx.deps.get('removed_peer') or {}
    removed_peer_user_id = removed_peer_row.get('user_id')

    # Authorization: self/linked peer removal or admin
    if removed_peer_user_id and removed_peer_user_id == signer_user_id:
        pass
    elif not signer_is_admin:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Core write: insert into removed_peers table
    # Note: removed_peers is device-wide (no recorded_by column)
    writes = [
        WriteOp(
            op='insert',
            table='removed_peers',
            values={
                'peer_shared_id': removed_peer_shared_id,
                'removed_at': removed_at,
                'signed_by': signed_by,
            },
        ),
    ]

    # Side effects: delete connections, rotate group keys
    commands = (
        Command(
            command_type='handle_peer_removed_side_effects',
            args={'removed_peer_shared_id': removed_peer_shared_id}
        ),
    )

    return ProjectorResult(writes=tuple(writes), valid_event=True, commands=commands)


def _wire_shadow_peer_removed(removed_peer_shared_id: str) -> None:
    """Validate peer_removed fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        removed_peer_id=crypto.b64decode(removed_peer_shared_id),
    )
    decoded = decode_plaintext(plaintext)
    if decoded["removed_peer_id"] != crypto.b64decode(removed_peer_shared_id):
        raise ValueError("wire shadow decode removed_peer_id mismatch")


def _rotate_keys_for_removed_peer_user(removed_user_id: str, recorded_by: str, t_ms: int, db: Any) -> None:
    """Rotate group keys if this was the last peer of a removed user.

    This ensures the removed user (via their last peer) cannot decrypt future messages.

    Args:
        removed_user_id: User ID whose last peer was removed
        recorded_by: Peer ID performing the removal
        t_ms: Timestamp for key creation
        db: Database connection
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Find all groups this user was a member of
    group_memberships = safedb.query(
        "SELECT DISTINCT group_id FROM group_members WHERE user_id = ? AND recorded_by = ?",
        (removed_user_id, recorded_by)
    )

    if not group_memberships:
        log.info(f"peer_removed._rotate_keys_for_removed_peer_user() user {removed_user_id[:20]}... was not a member of any groups")
        return

    log.info(f"peer_removed._rotate_keys_for_removed_peer_user() rotating keys for {len(group_memberships)} groups")

    # Get remover's peer_shared_id for signing rotated keys
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (recorded_by, recorded_by)
    )
    if not peer_self_row:
        log.warning(f"peer_removed._rotate_keys_for_removed_peer_user() could not find peer_shared_id for {recorded_by[:20]}..., skipping key rotation")
        return

    peer_shared_id = peer_self_row['peer_shared_id']

    # Rotate key for each group
    from events.group import group_key

    for group_row in group_memberships:
        group_id = group_row['group_id']
        try:
            group_key.rotate_for_removal(
                group_id=group_id,
                peer_id=recorded_by,
                peer_shared_id=peer_shared_id,
                t_ms=t_ms,  # No offset needed - DAG deps handle ordering
                removed_user_id=removed_user_id,
                db=db
            )
            log.info(f"peer_removed._rotate_keys_for_removed_peer_user() rotated key for group {group_id[:20]}...")
        except Exception as e:
            log.warning(f"peer_removed._rotate_keys_for_removed_peer_user() failed to rotate key for group {group_id[:20]}...: {e}")
            # Continue rotating keys for other groups even if one fails


def _handle_peer_removed_side_effects(args: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Command handler for peer_removed side effects.

    1. Delete all connections with the removed peer
    2. Rotate group keys if this was a user's peer
    """
    from events.network import connection_request

    removed_peer_shared_id = args.get('removed_peer_shared_id')
    if not removed_peer_shared_id:
        log.warning("handle_peer_removed_side_effects: missing removed_peer_shared_id")
        return

    # Delete connections
    deleted_count = connection_request.remove_connections_for_peer(removed_peer_shared_id, db)
    log.info(f"handle_peer_removed_side_effects: deleted {deleted_count} connection(s) for removed peer {removed_peer_shared_id[:20]}...")

    # Rotate group keys - lookup user_id for the removed peer
    safedb = create_safe_db(db, recorded_by=recorded_by)
    peer_row = safedb.query_one(
        "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
        (removed_peer_shared_id, recorded_by)
    )
    if peer_row and peer_row.get('user_id'):
        removed_user_id = peer_row['user_id']
        log.info(f"handle_peer_removed_side_effects: rotating keys for user {removed_user_id[:20]}...")
        _rotate_keys_for_removed_peer_user(removed_user_id, recorded_by, recorded_at, db)


# Register command handler at module load
register_command_handler('handle_peer_removed_side_effects', _handle_peer_removed_side_effects)
