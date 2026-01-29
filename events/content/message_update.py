"""Message update event type (shareable, encrypted).

Allows message authors to edit the text content of their messages.
Uses global_count for convergent update ordering (highest wins).
"""
from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from events.content import message
from events.identity import peer_shared, peer
from events.group import group as group_module
from core import global_counter
from core.db import create_safe_db, create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# v2 event specification - signed by peer_shared, encrypted
EVENT_SPEC = {
    'encrypted': True,
    'signer': {
        'id_field': 'edited_by',
        'type_field': 'signer_type',
    },
    'requires': {
        'message': {
            'source': 'table',
            'table': 'messages',
            'key': 'message_id',
            'fields': ['message_id', 'author_id', 'group_id'],
        },
    },
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for message_update events."""
    event_data = ctx.event_data

    message_id = event_data.get('message_id')
    group_id = event_data.get('group_id')
    edited_by = event_data.get('edited_by')
    author_id = event_data.get('author_id')
    global_count = event_data.get('global_count')
    new_content = event_data.get('new_content')
    created_at = event_data.get('created_at')

    if not all([message_id, group_id, edited_by, author_id, global_count is not None, new_content, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    if not new_content.strip():
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_message_update(
        message_id=message_id,
        group_id=group_id,
        edited_by=edited_by,
        author_id=author_id,
        new_content=new_content,
    )

    message_row = ctx.deps.get('message')
    if not message_row:
        return ProjectorResult(writes=tuple(), valid_event=False)
    if message_row.get('author_id') != author_id:
        return ProjectorResult(writes=tuple(), valid_event=False)
    if message_row.get('group_id') != group_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='insert',
            table='message_updates',
            values={
                'update_id': ctx.event_id,
                'message_id': message_id,
                'group_id': group_id,
                'edited_by': edited_by,
                'author_id': author_id,
                'global_count': global_count,
                'new_content': new_content,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)

# Event type registration
EVENT_TYPE = 'message_update'
SHAREABLE = True
PROJECTION_TABLE = ('message_updates', 'update_id')

# Wire format constants
WIRE_TYPE_CODE = 0x03  # TYPE_MESSAGE_UPDATE
WIRE_PLAINTEXT_SIZE = 344  # MESSAGE_UPDATE_PLAINTEXT_SIZE
UPDATE_MAX = 256


# Wire format functions - encode/decode for message_update event type

def encode_plaintext(
    message_id: bytes,
    group_id: bytes,
    edited_by: bytes,
    author_id: bytes,
    new_content: str | bytes,
) -> bytes:
    """Encode a message_update payload plaintext (pre-encryption).

    Layout (344 bytes):
    - message_id (16)
    - group_id (16)
    - edited_by (16)
    - author_id (16)
    - new_content_len (u16)
    - new_content_bytes (UPDATE_MAX)
    - pad
    """
    wire_format._require_len("message_id", message_id, 16)
    wire_format._require_len("group_id", group_id, 16)
    wire_format._require_len("edited_by", edited_by, 16)
    wire_format._require_len("author_id", author_id, 16)

    if isinstance(new_content, str):
        content_bytes = new_content.encode("utf-8")
    else:
        content_bytes = bytes(new_content)

    if len(content_bytes) > UPDATE_MAX:
        raise ValueError(f"new_content exceeds {UPDATE_MAX} bytes, got {len(content_bytes)}")

    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = message_id
    payload[16:32] = group_id
    payload[32:48] = edited_by
    payload[48:64] = author_id
    struct.pack_into("<H", payload, 64, len(content_bytes))
    payload[66:66 + len(content_bytes)] = content_bytes
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a message_update payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"message_update plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )

    message_id = data[0:16]
    group_id = data[16:32]
    edited_by = data[32:48]
    author_id = data[48:64]
    (content_len,) = struct.unpack_from("<H", data, 64)

    if content_len > UPDATE_MAX:
        raise ValueError(f"new_content_len exceeds {UPDATE_MAX}, got {content_len}")

    content_bytes = data[66:66 + content_len]
    try:
        new_content = content_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("new_content is not valid utf-8") from exc

    return {
        "message_id": message_id,
        "group_id": group_id,
        "edited_by": edited_by,
        "author_id": author_id,
        "new_content": new_content,
    }


def _encrypt_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    """Encrypt message_update plaintext into wire payload."""
    if len(plaintext) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"message_update plaintext must be {WIRE_PLAINTEXT_SIZE} bytes")
    key_id = wire_format._require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("message_update payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != WIRE_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for message_update payload")
    payload = key_id + nonce + ciphertext
    return wire_format._require_len("payload", payload, wire_format.PAYLOAD_SIZE)


def _decrypt_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    """Decrypt wire payload to message_update plaintext."""
    if key_data.get("type") != "symmetric":
        raise ValueError("message_update payload requires symmetric key")
    key_id = payload[:16]
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a message_update wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    message_id_b64: str,
    group_id_b64: str,
    edited_by_b64: str,
    author_id_b64: str,
    signer_type: str,
    new_content: str,
    global_count: int,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    """Encode a complete message_update wire event."""
    if global_count < 0 or global_count > 0xFFFFFFFF:
        raise ValueError("global_count must fit in u32")

    message_id = crypto.b64decode(message_id_b64)
    group_id = crypto.b64decode(group_id_b64)
    edited_by = crypto.b64decode(edited_by_b64)
    author_id = crypto.b64decode(author_id_b64)

    plaintext = encode_plaintext(
        message_id=message_id,
        group_id=group_id,
        edited_by=edited_by,
        author_id=author_id,
        new_content=new_content,
    )
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_ENCRYPTED,
        signer_type=wire_format.signer_type_from_str(signer_type),
        count=global_count,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=wire_format._require_len("signer_id", edited_by, wire_format.SIGNER_ID_SIZE),
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
    """Decode a message_update wire event."""
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
    if decoded["edited_by"] != header.signer_id:
        raise ValueError("edited_by does not match signer_id")
    event_data = {
        "type": EVENT_TYPE,
        "message_id": crypto.b64encode(decoded["message_id"]),
        "group_id": crypto.b64encode(decoded["group_id"]),
        "edited_by": crypto.b64encode(decoded["edited_by"]),
        "author_id": crypto.b64encode(decoded["author_id"]),
        "new_content": decoded["new_content"],
        "global_count": header.count,
        "created_at": header.created_at_ms,
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }
    return event_data, []


def create(
    message_id: str,
    new_content: str,
    peer_id: str,
    t_ms: int,
    db: Any,
) -> str:
    """Create a message update event (encrypted, shareable).

    Only the original message author can edit their messages.

    Args:
        message_id: Message to update
        new_content: New message content
        peer_id: Local peer ID (for signing and seeing)
        t_ms: Timestamp
        db: Database connection

    Returns:
        update_id: The created update event ID

    Raises:
        ValueError: If not authorized, message not found, or invalid parameters
    """
    # Get the original message to verify ownership and get group_id
    message_row = message.get(message_id, peer_id, db)
    if not message_row:
        raise ValueError(f"Message {message_id} not found")

    group_id = message_row['group_id']
    original_author_id = message_row['author_id']

    # Get our identity from peer_self
    identity = peer_shared.get_self(peer_id, db)
    if not identity or not identity['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found in peer_self table")
    if not identity['user_id']:
        raise ValueError(f"User identity not set for peer {peer_id}")

    peer_shared_id = identity['peer_shared_id']
    user_id = identity['user_id']

    # Check authorization - only the original author can edit
    if user_id != original_author_id:
        raise ValueError("Only the message author can edit their messages")

    # Validate new content
    if not new_content.strip():
        raise ValueError("Message content cannot be empty")

    # Get global count from framework (Lamport clock)
    global_count = global_counter.get_next_global_count(peer_id, db)

    _wire_shadow_message_update(
        message_id=message_id,
        group_id=group_id,
        edited_by=peer_shared_id,
        author_id=user_id,
        new_content=new_content,
    )

    private_key = peer.get_private_key(peer_id, peer_id, db)
    key_data = group_module.pick_key(group_id, peer_id, db)
    blob = encode_wire_event(
        message_id_b64=message_id,
        group_id_b64=group_id,
        edited_by_b64=peer_shared_id,
        author_id_b64=user_id,
        signer_type="peer_shared",
        new_content=new_content,
        global_count=global_count,
        created_at_ms=t_ms,
        key_data=key_data,
        private_key=private_key,
    )
    event_id = store.event(blob, peer_id, t_ms, db)

    log.info(
        f"message_update.create() created update_id={event_id} for message_id={message_id}, "
        f"global_count={global_count}"
    )

    return event_id


def _wire_shadow_message_update(
    message_id: str,
    group_id: str,
    edited_by: str,
    author_id: str,
    new_content: str,
) -> None:
    """Validate message_update fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        message_id=crypto.b64decode(message_id),
        group_id=crypto.b64decode(group_id),
        edited_by=crypto.b64decode(edited_by),
        author_id=crypto.b64decode(author_id),
        new_content=new_content,
    )
    decoded = decode_plaintext(plaintext)
    if decoded["new_content"] != new_content:
        raise ValueError("wire shadow decode new_content mismatch")


def get(message_id: str, recorded_by: str, db: Any) -> dict | None:
    """Get the current (winning) update for a message if any.

    Uses window function to get the update with highest global_count
    (and highest update_id as tiebreaker).

    Args:
        message_id: Message to get update for
        recorded_by: Peer perspective
        db: Database connection

    Returns:
        Dict with update info or None if no updates
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Use get_winners() from framework (update_id is the primary key, not event_id)
    result = global_counter.get_winners(
        'message_updates',
        'message_id',
        {'message_id': [message_id], 'recorded_by': recorded_by},
        db,
        id_field='update_id'
    )

    return result[0] if result else None


def list_history(message_id: str, recorded_by: str, db: Any) -> list[dict]:
    """List all updates for a message in chronological order.

    Args:
        message_id: Message to get history for
        recorded_by: Peer perspective
        db: Database connection

    Returns:
        List of update dicts ordered by global_count ascending
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return safedb.query(
        """SELECT mu.*, u.name as editor_name
           FROM message_updates mu
           LEFT JOIN users u ON mu.author_id = u.user_id AND mu.recorded_by = u.recorded_by
           WHERE mu.message_id = ? AND mu.recorded_by = ?
           ORDER BY mu.global_count ASC""",
        (message_id, recorded_by)
    )
