"""Group event type (shareable, encrypted)."""
from __future__ import annotations

# Registry metadata
EVENT_TYPE = 'group'
SHAREABLE = True  # Groups sync for access control
PROJECTION_TABLE = ('groups', 'group_id')

# Wire format constants
WIRE_TYPE_CODE = 0x10  # TYPE_GROUP
WIRE_PLAINTEXT_SIZE = 344  # GROUP_PLAINTEXT_SIZE
NAME_MAX = 64

from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from events.group import group_key
from events.identity import peer
from core.db import create_safe_db, create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp, Command

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


# Wire format functions - encode/decode for group event type

def encode_plaintext(
    name: str | bytes,
    key_id: bytes,
    is_main: int | bool,
    network_id: bytes | None,
) -> bytes:
    """Encode a group payload plaintext (pre-encryption).

    Layout (344 bytes):
    - name_len (u16)
    - name (NAME_MAX)
    - key_id (16)
    - is_main (1)
    - network_id (16)
    - pad
    """
    wire_format._require_len("key_id", key_id, 16)
    if isinstance(name, str):
        name_bytes = name.encode("utf-8")
    else:
        name_bytes = bytes(name)
    if not name_bytes:
        raise ValueError("group name must not be empty")
    if len(name_bytes) > NAME_MAX:
        raise ValueError(f"name exceeds {NAME_MAX} bytes, got {len(name_bytes)}")
    network_bytes = network_id or (b"\x00" * 16)
    wire_format._require_len("network_id", network_bytes, 16)

    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    struct.pack_into("<H", payload, 0, len(name_bytes))
    payload[2:2 + len(name_bytes)] = name_bytes
    payload[2 + NAME_MAX:2 + NAME_MAX + 16] = key_id
    payload[18 + NAME_MAX] = 1 if is_main else 0
    payload[19 + NAME_MAX:19 + NAME_MAX + 16] = network_bytes
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a group payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"group plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}")
    (name_len,) = struct.unpack_from("<H", data, 0)
    if name_len > NAME_MAX:
        raise ValueError(f"name_len exceeds {NAME_MAX}, got {name_len}")
    name_bytes = data[2:2 + name_len]
    try:
        name = name_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("name is not valid utf-8") from exc
    key_id = data[2 + NAME_MAX:2 + NAME_MAX + 16]
    is_main = data[18 + NAME_MAX]
    network_id = data[19 + NAME_MAX:19 + NAME_MAX + 16]
    if network_id == b"\x00" * 16:
        network_id = None
    return {
        "name": name,
        "key_id": key_id,
        "is_main": is_main,
        "network_id": network_id,
    }


def _encrypt_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    """Encrypt group plaintext into wire payload."""
    if len(plaintext) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"group plaintext must be {WIRE_PLAINTEXT_SIZE} bytes")
    key_id = wire_format._require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("group payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != WIRE_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for group payload")
    payload = key_id + nonce + ciphertext
    return wire_format._require_len("payload", payload, wire_format.PAYLOAD_SIZE)


def _decrypt_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    """Decrypt wire payload to group plaintext."""
    if key_data.get("type") != "symmetric":
        raise ValueError("group payload requires symmetric key")
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a group wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    name: str,
    key_id_b64: str,
    is_main: int | bool,
    network_id_b64: str | None,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    """Encode a complete group wire event."""
    key_id = crypto.b64decode(key_id_b64)
    network_id = crypto.b64decode(network_id_b64) if network_id_b64 else None
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_plaintext(
        name=name,
        key_id=key_id,
        is_main=is_main,
        network_id=network_id,
    )
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_ENCRYPTED,
        signer_type=wire_format.signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=wire_format._require_len("signer_id", signer_id, wire_format.SIGNER_ID_SIZE),
    )
    signed_bytes = wire_format._signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_payload(plaintext, key_data)
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Decode a group wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        return None, []
    if header.flags & wire_format.FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_payload(payload, key_data)
    else:
        plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    event_data = {
        "type": EVENT_TYPE,
        "name": decoded["name"],
        "key_id": crypto.b64encode(decoded["key_id"]),
        "is_main": decoded["is_main"],
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }
    if decoded["network_id"]:
        event_data["network_id"] = crypto.b64encode(decoded["network_id"])
    return event_data, []


def _seal_key_to_active_invites(key_id: str, peer_id: str, peer_shared_id: str, t_ms: int, db: Any) -> int:
    """Seal a group key to all active peer invites for this user.

    When a group is created, its key must be sealed to all active device-link
    invites so that devices joining via any invite can decrypt the key.

    Args:
        key_id: The group key to seal
        peer_id: Local peer ID
        peer_shared_id: Peer's shared identity
        t_ms: Timestamp
        db: Database connection

    Returns:
        Number of invites the key was sealed to
    """
    from events.group import group_key_shared

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get user_id from peer_self
    peer_self = safedb.query_one(
        "SELECT user_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
        (peer_id, peer_id)
    )
    if not peer_self or not peer_self['user_id']:
        return 0

    user_id = peer_self['user_id']

    # Find all mode='peer' invites for this user
    invites = safedb.query(
        "SELECT invite_id FROM invites WHERE mode = 'peer' AND user_id = ? AND recorded_by = ?",
        (user_id, peer_id)
    )

    sealed_count = 0
    for invite_row in invites:
        invite_id = invite_row['invite_id']
        try:
            group_key_shared.create_for_invite(
                key_id=key_id,
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                invite_id=invite_id,
                t_ms=t_ms,
                db=db
            )
            sealed_count += 1
            log.info(f"group._seal_key_to_active_invites() sealed key {key_id[:20]}... to invite {invite_id[:20]}...")
        except Exception as e:
            log.warning(f"group._seal_key_to_active_invites() failed to seal key to invite {invite_id[:20]}...: {e}")

    if sealed_count > 0:
        log.info(f"group._seal_key_to_active_invites() sealed key to {sealed_count} active invite(s)")

    return sealed_count


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

    blob = encode_wire_event(
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

    # Seal key to all active peer invites for this user
    # This ensures devices joining via any invite can decrypt this group's key
    _seal_key_to_active_invites(key_id, peer_id, peer_shared_id, t_ms, db)

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
    plaintext = encode_plaintext(
        name=name,
        key_id=crypto.b64decode(key_id),
        is_main=is_main,
        network_id=crypto.b64decode(network_id) if network_id else None,
    )
    decoded = decode_plaintext(plaintext)
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
