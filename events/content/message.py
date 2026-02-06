"""Message event type."""

# Registry metadata
EVENT_TYPE = 'message'
SHAREABLE = True  # Messages sync to channel members
PROJECTION_TABLE = ('messages', 'message_id')

# Wire format constants
WIRE_TYPE_CODE = 0x01  # TYPE_MESSAGE
WIRE_PLAINTEXT_SIZE = 344  # MESSAGE_PLAINTEXT_SIZE
CONTENT_MAX = 256

from typing import Any
import struct
import logging
from core import crypto
from core import store
from core import wire_format
from events.group import group
from events.identity import peer, peer_shared, user
from events.content import channel
from core.db import create_safe_db, create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)

# Default message TTL: 1 week (in milliseconds)
DEFAULT_MESSAGE_TTL_MS = 7 * 24 * 60 * 60 * 1000


# Wire format functions - encode/decode for message event type

def encode_plaintext(
    channel_id: bytes,
    author_id: bytes,
    content: str | bytes,
    disappearing_time_ms: int,
) -> bytes:
    """Encode a message payload plaintext (pre-encryption).

    Layout (344 bytes):
    - channel_id (16)
    - author_id (16)
    - disappearing_time_ms (u64)
    - content_len (u16)
    - content_bytes (CONTENT_MAX)
    - pad
    """
    wire_format._require_len("channel_id", channel_id, 16)
    wire_format._require_len("author_id", author_id, 16)

    if isinstance(content, str):
        content_bytes = content.encode("utf-8")
    else:
        content_bytes = bytes(content)

    if len(content_bytes) > CONTENT_MAX:
        raise ValueError(f"content exceeds {CONTENT_MAX} bytes, got {len(content_bytes)}")

    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = channel_id
    payload[16:32] = author_id
    struct.pack_into("<Q", payload, 32, disappearing_time_ms)
    struct.pack_into("<H", payload, 40, len(content_bytes))
    payload[42:42 + len(content_bytes)] = content_bytes
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a message payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"message plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}")

    channel_id = data[0:16]
    author_id = data[16:32]
    (disappearing_time_ms,) = struct.unpack_from("<Q", data, 32)
    (content_len,) = struct.unpack_from("<H", data, 40)

    if content_len > CONTENT_MAX:
        raise ValueError(f"content_len exceeds {CONTENT_MAX}, got {content_len}")

    content_bytes = data[42:42 + content_len]
    try:
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("content is not valid utf-8") from exc

    return {
        "channel_id": channel_id,
        "author_id": author_id,
        "disappearing_time_ms": disappearing_time_ms,
        "content": content,
    }


def _encrypt_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    """Encrypt message plaintext into wire payload."""
    if len(plaintext) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"message plaintext must be {WIRE_PLAINTEXT_SIZE} bytes")
    key_id = wire_format._require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("message payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != WIRE_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for message payload")
    payload = key_id + nonce + ciphertext
    return wire_format._require_len("payload", payload, wire_format.PAYLOAD_SIZE)


def _decrypt_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    """Decrypt wire payload to message plaintext."""
    if key_data.get("type") != "symmetric":
        raise ValueError("message payload requires symmetric key")
    key_id = payload[:16]
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a message wire envelope."""
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
    author_id_b64: str,
    content: str,
    ttl_ms: int,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    """Encode a complete message wire event."""
    channel_id = crypto.b64decode(channel_id_b64)
    author_id = crypto.b64decode(author_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)

    plaintext = encode_plaintext(
        channel_id=channel_id,
        author_id=author_id,
        content=content,
        disappearing_time_ms=ttl_ms,
    )
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_ENCRYPTED,
        signer_type=wire_format.signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=ttl_ms,
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
    """Decode a message wire event."""
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
        "channel_id": crypto.b64encode(decoded["channel_id"]),
        "author_id": crypto.b64encode(decoded["author_id"]),
        "disappearing_time_ms": decoded["disappearing_time_ms"],
        "content": decoded["content"],
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }
    return event_data, []


def _wire_shadow_message(channel_id: str, author_id: str, content: str, disappearing_time_ms: int) -> None:
    """Validate message fields against the fixed-size wire payload layout."""
    channel_id_bytes = crypto.b64decode(channel_id)
    author_id_bytes = crypto.b64decode(author_id)
    plaintext = encode_plaintext(
        channel_id=channel_id_bytes,
        author_id=author_id_bytes,
        content=content,
        disappearing_time_ms=disappearing_time_ms,
    )
    decoded = decode_plaintext(plaintext)
    if decoded["content"] != content:
        raise ValueError("wire shadow decode content mismatch")


# v2 event specification - signed by peer_shared, encrypted
EVENT_SPEC = {
    'encrypted': True,
    'skip_if_deleted': True,  # Skip projection if in deleted_events (deletion arrived first)
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {
        'channel': {
            'source': 'table',
            'table': 'channels',
            'key': 'channel_id',
            'fields': ['group_id'],
        },
        'author': {
            'source': 'table',
            'table': 'users',
            'key': 'user_id',
            'key_from': 'author_id',
            'fields': ['user_id'],
        },
    },
    'optional': {
        'store_blob': {
            'source': 'context',
            'table': 'store',
            'key': 'id',
            'key_from': '@event_id',
            'fields': ['blob'],
        },
    },
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for message events."""
    event_data = ctx.event_data

    channel_id = event_data.get('channel_id')
    author_id = event_data.get('author_id')
    signed_by = event_data.get('signed_by')
    content = event_data.get('content')
    created_at = event_data.get('created_at')
    disappearing_time_ms = event_data.get('disappearing_time_ms', 0) or 0

    if not channel_id or not author_id or not signed_by or created_at is None or content is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_message(channel_id, author_id, content, disappearing_time_ms)

    channel_row = ctx.deps.get('channel')
    if not channel_row or not channel_row.get('group_id'):
        return ProjectorResult(writes=tuple(), valid_event=False)

    store_row = ctx.deps.get('store_blob') or {}
    event_blob = store_row.get('blob')
    if not event_blob:
        return ProjectorResult(writes=tuple(), valid_event=False)

    group_id = channel_row['group_id']

    if disappearing_time_ms > 0:
        ttl_ms = created_at + disappearing_time_ms
    else:
        ttl_ms = 0

    if not is_wire_envelope(event_blob):
        return ProjectorResult(writes=tuple(), valid_event=False)
    _header, payload, _signature = wire_format.parse_envelope(event_blob)
    key_id_bytes = payload[:crypto.KEY_ID_SIZE]
    key_id_b64 = crypto.b64encode(key_id_bytes)

    writes = (
        WriteOp(
            op='insert',
            table='messages',
            values={
                'message_id': ctx.event_id,
                'channel_id': channel_id,
                'group_id': group_id,
                'author_id': author_id,
                'signed_by': signed_by,
                'content': content,
                'created_at': created_at,
                'ttl_ms': ttl_ms,
                'key_id': key_id_b64,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
        WriteOp(
            op='insert',
            table='event_dependencies',
            values={
                'child_event_id': ctx.event_id,
                'parent_event_id': channel_id,
                'recorded_by': ctx.recorded_by,
                'dependency_type': 'channel',
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def create(peer_id: str, channel_id: str, content: str, t_ms: int, db: Any, return_latest: bool = True) -> dict[str, Any]:
    """Create a message event, add it to the store, project it, and return the id and a list of recent messages.

    Message TTL is determined by the channel's disappearing_time_ms setting:
    - If disappearing_time_ms is 0: message is permanent (ttl_ms = 0)
    - If disappearing_time_ms > 0: message expires at created_at + disappearing_time_ms

    SECURITY: This function trusts that peer_id is correct and owned by the caller.
    In production, the API authentication layer should validate that the authenticated session
    owns this peer_id before calling this function. This is safe for local-only apps where
    the user controls all peers on the device.

    Args:
        peer_id: Local peer ID creating the message
        channel_id: Channel to post message in
        content: Message content
        t_ms: Timestamp
        db: Database connection
        return_latest: If True (default), return list of recent messages. Set to False for bulk creation.

    Returns:
        {'id': message_id, 'latest': list of recent messages (or empty list if return_latest=False)}
    """
    log.info(f"message.create() creating message in channel_id={channel_id}, content='{content[:50]}...'")

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Query channel to get group_id and disappearing_time_ms
    channel_row = channel.get(channel_id, peer_id, db)
    if not channel_row:
        raise ValueError(f"Channel {channel_id} not found for peer {peer_id}")

    group_id = channel_row['group_id']
    disappearing_time_ms = channel_row.get('disappearing_time_ms', 0) or 0

    # Look up our identity from peer_self (set when we created/linked our account)
    # This is the canonical source for "what user am I?" - single lookup, no fallback chain
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id, user_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_id, peer_id)
    )
    if not peer_self_row or not peer_self_row['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found in peer_self table")
    if not peer_self_row['user_id']:
        raise ValueError(f"User identity not set for peer {peer_id}. Must create or link account first.")

    peer_shared_id = peer_self_row['peer_shared_id']
    user_id = peer_self_row['user_id']

    # Check if user has been removed from the network
    removal_row = safedb.query_one(
        "SELECT 1 FROM removed_users WHERE user_id = ? AND recorded_by = ? LIMIT 1",
        (user_id, peer_id)
    )
    if removal_row:
        raise ValueError(f"User {user_id} has been removed from the network and cannot send messages.")

    # Check that user has a username set before allowing message creation
    # This ensures the author's name can be displayed with their message
    username_row = safedb.query_one(
        "SELECT event_id FROM user_names WHERE user_id = ? AND recorded_by = ? LIMIT 1",
        (user_id, peer_id)
    )
    if not username_row:
        raise ValueError(f"No username found for user {user_id}. Username must be set before sending messages.")

    _wire_shadow_message(channel_id, user_id, content, disappearing_time_ms)

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)

    # Get key_data for encryption (group.pick_key uses peer_id for access control)
    try:
        key_data = group.pick_key(group_id, peer_id, db)
    except group.KeyUnsafeError as e:
        # Key is not safe - check if we're an admin
        from events.identity import invite
        if invite.is_admin(peer_shared_id, peer_id, db):
            # Admin: rotate key first
            log.info(f"message.create() key unsafe, admin rotating: {e}")
            from events.group import group_key
            new_key_id = group_key.rotate_for_split_brain(
                group_id=e.group_id,
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                t_ms=t_ms,
                db=db
            )
            key_data = group_key.get_key(new_key_id, peer_id, db)
        else:
            # Non-admin: cannot send
            raise ValueError(f"Cannot send message: {e}. Waiting for admin to rotate key.")

    blob = encode_wire_event(
        channel_id_b64=channel_id,
        author_id_b64=user_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        content=content,
        created_at_ms=t_ms,
        ttl_ms=disappearing_time_ms,
        key_data=key_data,
        private_key=private_key,
    )

    # Store event with recorded wrapper and projection
    event_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"message.create() created message_id={event_id}")

    # Get latest messages (skip for bulk creation performance)
    latest = list(channel_id, peer_id, db) if return_latest else []

    # Note: No commit here - caller owns the transaction (future API layer or tests)

    return {
        'id': event_id,
        'latest': latest
    }


def get(message_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get a single message by ID.

    Args:
        message_id: Message ID to look up
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        Message dict with all fields, or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return safedb.query_one(
        """SELECT * FROM messages WHERE message_id = ? AND recorded_by = ?""",
        (message_id, recorded_by)
    )


def batch_create(peer_id: str, channel_id: str, contents: list[str], start_t_ms: int, db: Any,
                 skip_projection: bool = False) -> list[str]:
    """Create multiple messages efficiently with cached lookups.

    Optimized for bulk message creation (e.g., perf tests). Caches DB lookups
    and crypto keys, then creates all messages in a batch.

    Args:
        peer_id: Local peer ID creating the messages
        channel_id: Channel to post messages in
        contents: List of message contents
        start_t_ms: Starting timestamp (increments by 1 for each message)
        db: Database connection
        skip_projection: If True, skip projection (messages won't sync). Default False.

    Returns:
        List of message IDs
    """
    from events.identity import peer as peer_module
    from events.group import group

    if not contents:
        return []

    log.info(f"message.batch_create() creating {len(contents)} messages in channel_id={channel_id}")

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Cache lookups (done once for all messages)
    channel_row = safedb.query_one(
        "SELECT group_id, disappearing_time_ms FROM channels WHERE channel_id = ? AND recorded_by = ? LIMIT 1",
        (channel_id, peer_id)
    )
    if not channel_row:
        raise ValueError(f"Channel {channel_id} not found for peer {peer_id}")

    group_id = channel_row['group_id']
    disappearing_time_ms = channel_row.get('disappearing_time_ms', 0) or 0

    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id, user_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_id, peer_id)
    )
    if not peer_self_row or not peer_self_row['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found in peer_self table")
    if not peer_self_row['user_id']:
        raise ValueError(f"User identity not set for peer {peer_id}.")

    peer_shared_id = peer_self_row['peer_shared_id']
    user_id = peer_self_row['user_id']

    # Check removed users
    removal_row = safedb.query_one(
        "SELECT 1 FROM removed_users WHERE user_id = ? AND recorded_by = ? LIMIT 1",
        (user_id, peer_id)
    )
    if removal_row:
        raise ValueError(f"User {user_id} has been removed from the network.")

    # Check username exists
    username_row = safedb.query_one(
        "SELECT event_id FROM user_names WHERE user_id = ? AND recorded_by = ? LIMIT 1",
        (user_id, peer_id)
    )
    if not username_row:
        raise ValueError(f"No username found for user {user_id}.")

    # Cache crypto keys
    private_key = peer_module.get_private_key(peer_id, peer_id, db)
    key_data = group.pick_key(group_id, peer_id, db)

    # Build all event blobs
    event_blobs = []
    for i, content in enumerate(contents):
        t_ms = start_t_ms + i

        _wire_shadow_message(channel_id, user_id, content, disappearing_time_ms)

        blob = encode_wire_event(
            channel_id_b64=channel_id,
            author_id_b64=user_id,
            signed_by_b64=peer_shared_id,
            signer_type="peer_shared",
            content=content,
            created_at_ms=t_ms,
            ttl_ms=disappearing_time_ms,
            key_data=key_data,
            private_key=private_key,
        )
        event_blobs.append(blob)

    if skip_projection:
        # Batch store without projection (fastest, but messages won't sync)
        event_ids = store.batch_store_events(event_blobs, peer_id, start_t_ms, db)
    else:
        # Store with projection (slower, but messages will sync correctly)
        event_ids = []
        for i, blob in enumerate(event_blobs):
            t_ms = start_t_ms + i
            event_id = store.event(blob, peer_id, t_ms, db)
            event_ids.append(event_id)

    log.info(f"message.batch_create() created {len(event_ids)} messages (skip_projection={skip_projection})")
    return event_ids


def list(channel_id: int, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List messages in a channel for a specific peer, including attachments, author names, and reactions.

    Returns message dicts with 'attachments' field containing list of attached files,
    'author_name' field from the user_names table, and 'reactions' field from message_reactions:
    [{'message_id', 'content', 'author_id', 'author_name', 'created_at', 'attachments': [...], 'reactions': [...]}, ...]
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    messages = safedb.query(
        """SELECT m.*, un.name as author_name
           FROM messages m
           LEFT JOIN user_names un ON m.author_id = un.user_id AND m.recorded_by = un.recorded_by
           WHERE m.channel_id = ? AND m.recorded_by = ?
           ORDER BY m.created_at ASC LIMIT 50""",
        (channel_id, recorded_by)
    )

    # Enrich each message with attachments, reactions, and apply any winning updates
    from events.content import message_reaction, message_update
    for msg in messages:
        # Apply any winning message update if it exists
        winning_update = message_update.get(msg['message_id'], recorded_by, db)
        if winning_update:
            msg['content'] = winning_update['new_content']
            msg['edited_at'] = winning_update['created_at']
        else:
            msg['edited_at'] = 0

        attachments = safedb.query(
            """SELECT ma.file_id, ma.filename, ma.mime_type, ma.blob_bytes
               FROM message_attachments ma
               WHERE ma.message_id = ? AND ma.recorded_by = ?
               ORDER BY ma.recorded_at ASC""",
            (msg['message_id'], recorded_by)
        )
        msg['attachments'] = attachments if attachments else []

        # Get reactions for this message
        reactions = message_reaction.list_reactions(msg['message_id'], recorded_by, db)
        msg['reactions'] = reactions if reactions else []

    return messages
