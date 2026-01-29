"""Channel update event type (shareable, encrypted).

Allows admins to update channel name or disappearing_time_ms after creation.
Uses global_count for convergent update ordering (highest wins).
"""

# Registry metadata
EVENT_TYPE = 'channel_update'
SHAREABLE = True  # Channel updates sync to all members
PROJECTION_TABLE = None

# Wire format constants
WIRE_TYPE_CODE = 0x0A  # TYPE_CHANNEL_UPDATE
WIRE_PLAINTEXT_SIZE = 344  # CHANNEL_UPDATE_PLAINTEXT_SIZE
NAME_MAX = 64

from typing import Any
import struct
import logging
from core import crypto
from core import store
from core import wire_format
from events.content import channel
from events.group import group
from events.identity import invite as invite_module, peer
from core.db import create_safe_db, create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for channel_update event type

def encode_plaintext(
    channel_id: bytes,
    group_id: bytes,
    updated_by: bytes,
    new_channel_name: str | bytes | None,
    new_disappearing_time_ms: int | None,
) -> bytes:
    """Encode a channel_update payload plaintext (pre-encryption).

    Layout (344 bytes):
    - channel_id (16)
    - group_id (16)
    - updated_by (16)
    - name_len (u16)
    - name_bytes (NAME_MAX)
    - new_disappearing_time_ms (u64) - 0xFFFFFFFFFFFFFFFF if None
    - pad
    """
    wire_format._require_len("channel_id", channel_id, 16)
    wire_format._require_len("group_id", group_id, 16)
    wire_format._require_len("updated_by", updated_by, 16)
    if new_channel_name is None:
        name_bytes = b""
    elif isinstance(new_channel_name, str):
        name_bytes = new_channel_name.encode("utf-8")
    else:
        name_bytes = bytes(new_channel_name)
    if len(name_bytes) > NAME_MAX:
        raise ValueError(f"new_channel_name exceeds {NAME_MAX} bytes, got {len(name_bytes)}")
    ttl_value = 0xFFFFFFFFFFFFFFFF if new_disappearing_time_ms is None else new_disappearing_time_ms
    if ttl_value < 0 or ttl_value > 0xFFFFFFFFFFFFFFFF:
        raise ValueError("new_disappearing_time_ms must fit in u64")

    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = channel_id
    payload[16:32] = group_id
    payload[32:48] = updated_by
    struct.pack_into("<H", payload, 48, len(name_bytes))
    payload[50:50 + len(name_bytes)] = name_bytes
    struct.pack_into("<Q", payload, 50 + NAME_MAX, ttl_value)
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a channel_update payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"channel_update plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    channel_id = data[0:16]
    group_id = data[16:32]
    updated_by = data[32:48]
    (name_len,) = struct.unpack_from("<H", data, 48)
    if name_len > NAME_MAX:
        raise ValueError(f"new_channel_name_len exceeds {NAME_MAX}, got {name_len}")
    name_bytes = data[50:50 + name_len]
    new_channel_name = name_bytes.decode("utf-8") if name_len else None
    (new_disappearing_time_ms,) = struct.unpack_from("<Q", data, 50 + NAME_MAX)
    if new_disappearing_time_ms == 0xFFFFFFFFFFFFFFFF:
        new_disappearing_time_ms = None
    return {
        "channel_id": channel_id,
        "group_id": group_id,
        "updated_by": updated_by,
        "new_channel_name": new_channel_name,
        "new_disappearing_time_ms": new_disappearing_time_ms,
    }


def _encrypt_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    """Encrypt channel_update plaintext into wire payload."""
    if len(plaintext) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"channel_update plaintext must be {WIRE_PLAINTEXT_SIZE} bytes")
    key_id = wire_format._require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("channel_update payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != WIRE_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for channel_update payload")
    payload = key_id + nonce + ciphertext
    return wire_format._require_len("payload", payload, wire_format.PAYLOAD_SIZE)


def _decrypt_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    """Decrypt wire payload to channel_update plaintext."""
    if key_data.get("type") != "symmetric":
        raise ValueError("channel_update payload requires symmetric key")
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a channel_update wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    channel_id_b64: str,
    group_id_b64: str,
    updated_by_b64: str,
    signer_type: str,
    new_channel_name: str | None,
    new_disappearing_time_ms: int | None,
    global_count: int,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    """Encode a complete channel_update wire event."""
    if global_count < 0 or global_count > 0xFFFFFFFF:
        raise ValueError("global_count must fit in u32")
    channel_id = crypto.b64decode(channel_id_b64)
    group_id = crypto.b64decode(group_id_b64)
    updated_by = crypto.b64decode(updated_by_b64)
    plaintext = encode_plaintext(
        channel_id=channel_id,
        group_id=group_id,
        updated_by=updated_by,
        new_channel_name=new_channel_name,
        new_disappearing_time_ms=new_disappearing_time_ms,
    )
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_ENCRYPTED,
        signer_type=wire_format.signer_type_from_str(signer_type),
        count=global_count,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=wire_format._require_len("signer_id", updated_by, wire_format.SIGNER_ID_SIZE),
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
    """Decode a channel_update wire event."""
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
    if decoded["updated_by"] != header.signer_id:
        raise ValueError("updated_by does not match signer_id")
    event_data = {
        "type": EVENT_TYPE,
        "channel_id": crypto.b64encode(decoded["channel_id"]),
        "group_id": crypto.b64encode(decoded["group_id"]),
        "updated_by": crypto.b64encode(decoded["updated_by"]),
        "new_channel_name": decoded["new_channel_name"],
        "new_disappearing_time_ms": decoded["new_disappearing_time_ms"],
        "global_count": header.count,
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }
    return event_data, []


EVENT_SPEC = {
    'encrypted': True,
    'signer': {
        'id_field': 'updated_by',
        'type_field': 'signer_type',
    },
    'requires': {
        'channel': {
            'source': 'table',
            'table': 'channels',
            'key': 'channel_id',
            'fields': ['channel_id', 'group_id'],
        },
    },
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for channel_update events."""
    event_data = ctx.event_data

    channel_id = event_data.get('channel_id')
    group_id = event_data.get('group_id')
    updated_by = event_data.get('updated_by')
    global_count = event_data.get('global_count')
    new_channel_name = event_data.get('new_channel_name')
    new_disappearing_time_ms = event_data.get('new_disappearing_time_ms')
    created_at = event_data.get('created_at')

    if not all([channel_id, group_id, updated_by, global_count is not None, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    if new_channel_name is None and new_disappearing_time_ms is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    if new_channel_name is not None and not new_channel_name.strip():
        return ProjectorResult(writes=tuple(), valid_event=False)

    if new_disappearing_time_ms is not None and new_disappearing_time_ms < 0:
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_channel_update(channel_id, group_id, updated_by, new_channel_name, new_disappearing_time_ms)

    channel_row = ctx.deps.get('channel')
    if not channel_row or channel_row.get('group_id') != group_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    signer = ctx.signer or {}
    if signer.get('type') != 'peer_shared':
        return ProjectorResult(writes=tuple(), valid_event=False)
    if not signer.get('is_admin'):
        return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='insert',
            table='channel_updates',
            values={
                'update_id': ctx.event_id,
                'channel_id': channel_id,
                'updated_by': updated_by,
                'global_count': global_count,
                'new_channel_name': new_channel_name,
                'new_disappearing_time_ms': new_disappearing_time_ms,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def _wire_shadow_channel_update(
    channel_id: str,
    group_id: str,
    updated_by: str,
    new_channel_name: str | None,
    new_disappearing_time_ms: int | None,
) -> None:
    """Validate channel_update fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        channel_id=crypto.b64decode(channel_id),
        group_id=crypto.b64decode(group_id),
        updated_by=crypto.b64decode(updated_by),
        new_channel_name=new_channel_name,
        new_disappearing_time_ms=new_disappearing_time_ms,
    )
    decoded = decode_plaintext(plaintext)
    if decoded["channel_id"] != crypto.b64decode(channel_id):
        raise ValueError("wire shadow decode channel_id mismatch")


def _validate_admin(peer_shared_id: str, recorded_by: str, db: Any) -> bool:
    """Validate that peer_shared_id is an admin.

    Args:
        peer_shared_id: Public peer ID to check
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        True if user is admin, False otherwise
    """
    return invite_module.is_admin(peer_shared_id, recorded_by, db)


def create(
    channel_id: str,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
    new_channel_name: str | None = None,
    new_disappearing_time_ms: int | None = None,
) -> str:
    """Create a channel update event (encrypted, shareable).

    Only admins can update channels. At least one of new_channel_name or
    new_disappearing_time_ms must be provided.

    Args:
        channel_id: Channel to update
        peer_id: Local peer ID (for signing and seeing)
        peer_shared_id: Public peer ID (for updated_by)
        t_ms: Timestamp
        db: Database connection
        new_channel_name: New channel name (or None to keep existing)
        new_disappearing_time_ms: New disappearing time in ms (or None to keep existing)

    Returns:
        update_id: The created update event ID

    Raises:
        ValueError: If not authorized, channel not found, or invalid parameters
    """
    # Check authorization - only admins can update channels
    if not _validate_admin(peer_shared_id, peer_id, db):
        raise ValueError(f"User {peer_shared_id} not authorized to update channels (only admins can)")

    # Validate at least one field is provided
    if new_channel_name is None and new_disappearing_time_ms is None:
        raise ValueError("At least one field (name or disappearing_time_ms) must be provided")

    # Get channel to find group_id
    channel_row = channel.get(channel_id, peer_id, db)
    if not channel_row:
        raise ValueError(f"Channel {channel_id} not found")

    group_id = channel_row['group_id']

    # Validate new values
    if new_channel_name is not None and not new_channel_name.strip():
        raise ValueError("Channel name cannot be empty")

    if new_disappearing_time_ms is not None and new_disappearing_time_ms < 0:
        raise ValueError("disappearing_time_ms must be non-negative")

    # Calculate global_count (max existing + 1)
    global_count = channel.get_next_update_count(channel_id, peer_id, db)

    _wire_shadow_channel_update(channel_id, group_id, peer_shared_id, new_channel_name, new_disappearing_time_ms)

    private_key = peer.get_private_key(peer_id, peer_id, db)
    key_data = group.pick_key(group_id, peer_id, db)
    blob = encode_wire_event(
        channel_id_b64=channel_id,
        group_id_b64=group_id,
        updated_by_b64=peer_shared_id,
        signer_type="peer_shared",
        new_channel_name=new_channel_name,
        new_disappearing_time_ms=new_disappearing_time_ms,
        global_count=global_count,
        created_at_ms=t_ms,
        key_data=key_data,
        private_key=private_key,
    )
    event_id = store.event(blob, peer_id, t_ms, db)

    log.info(
        f"channel_update.create() created update_id={event_id} for channel_id={channel_id}, "
        f"name={new_channel_name}, ttl={new_disappearing_time_ms}"
    )

    return event_id


def validate(update_id: str, recorded_by: str, db: Any) -> bool:
    """Validate a channel update event.

    Checks:
    1. Creator is an admin
    2. Channel exists
    3. Values are valid (non-negative disappearing_time, non-empty name if provided)

    Args:
        update_id: Event ID of the update
        recorded_by: Peer perspective for validation
        db: Database connection

    Returns:
        True if valid, False otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get the update from the table
    update_row = safedb.query_one(
        "SELECT * FROM channel_updates WHERE update_id = ? AND recorded_by = ? LIMIT 1",
        (update_id, recorded_by)
    )

    if not update_row:
        log.warning(f"channel_update.validate() update not found for update_id={update_id}")
        return False

    # Check admin authorization
    if not _validate_admin(update_row['updated_by'], recorded_by, db):
        log.warning(f"channel_update.validate() updater {update_row['updated_by']} is not admin")
        return False

    # Check channel exists
    channel_row = safedb.query_one(
        "SELECT channel_id FROM channels WHERE channel_id = ? AND recorded_by = ? LIMIT 1",
        (update_row['channel_id'], recorded_by)
    )

    if not channel_row:
        log.warning(f"channel_update.validate() channel not found for channel_id={update_row['channel_id']}")
        return False

    # Validate field values
    if (update_row['new_channel_name'] is not None and
        not update_row['new_channel_name'].strip()):
        log.warning(f"channel_update.validate() empty channel name in update_id={update_id}")
        return False

    if (update_row['new_disappearing_time_ms'] is not None and
        update_row['new_disappearing_time_ms'] < 0):
        log.warning(f"channel_update.validate() negative disappearing_time in update_id={update_id}")
        return False

    return True
