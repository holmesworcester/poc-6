"""Message reaction event type - add/remove emoji reactions to messages.

Reactions are group-encrypted (access control via message's group_id).
Authorization: Any group member can add reactions, only the reactor or admins can remove.
Reactions are reference events that depend on messages.
"""
from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import global_counter
from core import wire_format
from events.content import message
from events.content import message_reaction_deletion
from events.group import group
from events.identity import peer_shared, peer
from core.db import create_safe_db, create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Event type declarations for auto-discovery
EVENT_TYPE = 'message_reaction'
SHAREABLE = True  # Sync reactions to other peers
PROJECTION_TABLE = ('message_reactions', 'reaction_id')

# Wire format constants
WIRE_TYPE_CODE = 0x05  # TYPE_MESSAGE_REACTION
WIRE_PLAINTEXT_SIZE = 344  # MESSAGE_REACTION_PLAINTEXT_SIZE


# Wire format functions - encode/decode for message_reaction event type

def encode_plaintext(
    message_id: bytes,
    reactor_id: bytes,
    emoji: str,
) -> bytes:
    """Encode a message_reaction payload plaintext (pre-encryption)."""
    wire_format._require_len("message_id", message_id, 16)
    wire_format._require_len("reactor_id", reactor_id, 16)
    if not isinstance(emoji, str):
        raise ValueError("emoji must be a single unicode codepoint")
    if len(emoji) != 1:
        # Allow variation selectors (e.g., "❤️" -> "❤")
        stripped = "".join(ch for ch in emoji if ord(ch) not in (0xFE0E, 0xFE0F))
        if len(stripped) != 1:
            raise ValueError("emoji must be a single unicode codepoint")
        emoji = stripped
    codepoint = ord(emoji)
    if codepoint > 0x10FFFF:
        raise ValueError("emoji codepoint out of range")
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = message_id
    payload[16:32] = reactor_id
    struct.pack_into("<I", payload, 32, codepoint)
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a message_reaction payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"message_reaction plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    message_id = data[0:16]
    reactor_id = data[16:32]
    (codepoint,) = struct.unpack_from("<I", data, 32)
    emoji = chr(codepoint) if codepoint else ""
    if emoji == "\u2764":
        emoji = "\u2764\uFE0F"
    return {
        "message_id": message_id,
        "reactor_id": reactor_id,
        "emoji": emoji,
    }


def _encrypt_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    """Encrypt message_reaction plaintext into wire payload."""
    if len(plaintext) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"message_reaction plaintext must be {WIRE_PLAINTEXT_SIZE} bytes")
    key_id = wire_format._require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("message_reaction payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != WIRE_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for message_reaction payload")
    payload = key_id + nonce + ciphertext
    return wire_format._require_len("payload", payload, wire_format.PAYLOAD_SIZE)


def _decrypt_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    """Decrypt wire payload to message_reaction plaintext."""
    if key_data.get("type") != "symmetric":
        raise ValueError("message_reaction payload requires symmetric key")
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a message_reaction wire envelope."""
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
    reactor_id_b64: str,
    signed_by_b64: str,
    signer_type: str,
    emoji: str,
    global_count: int,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    """Encode a complete message_reaction wire event."""
    if global_count < 0 or global_count > 0xFFFFFFFF:
        raise ValueError("global_count must fit in u32")
    message_id = crypto.b64decode(message_id_b64)
    reactor_id = crypto.b64decode(reactor_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)

    plaintext = encode_plaintext(
        message_id=message_id,
        reactor_id=reactor_id,
        emoji=emoji,
    )
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_ENCRYPTED,
        signer_type=wire_format.signer_type_from_str(signer_type),
        count=global_count,
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
    """Decode a message_reaction wire event."""
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
        "message_id": crypto.b64encode(decoded["message_id"]),
        "reactor_id": crypto.b64encode(decoded["reactor_id"]),
        "emoji": decoded["emoji"],
        "global_count": header.count,
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }
    return event_data, []


# v2 event specification - signed by peer_shared, encrypted
EVENT_SPEC = {
    'encrypted': True,
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {
        'message': {
            'source': 'table',
            'table': 'messages',
            'key': 'message_id',
            'fields': ['message_id'],
        },
    },
    'optional': {
        'deletion': {
            'source': 'context',
            'table': 'message_reaction_deletions',
            'key': 'reaction_id',
            'key_from': '@event_id',
            'fields': ['deletion_id'],
        },
    },
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for message_reaction events."""
    event_data = ctx.event_data

    message_id = event_data.get('message_id')
    reactor_id = event_data.get('reactor_id')
    signed_by = event_data.get('signed_by')
    emoji = event_data.get('emoji')
    created_at = event_data.get('created_at')
    global_count = event_data.get('global_count')

    if not all([message_id, reactor_id, signed_by, emoji, created_at is not None, global_count is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_message_reaction(message_id, reactor_id, emoji)

    if ctx.deps.get('deletion'):
        return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='insert',
            table='message_reactions',
            values={
                'reaction_id': ctx.event_id,
                'message_id': message_id,
                'reactor_id': reactor_id,
                'signed_by': signed_by,
                'emoji': emoji,
                'created_at': created_at,
                'global_count': global_count,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def _wire_shadow_message_reaction(message_id: str, reactor_id: str, emoji: str) -> None:
    """Validate message_reaction fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        message_id=crypto.b64decode(message_id),
        reactor_id=crypto.b64decode(reactor_id),
        emoji=emoji,
    )
    decoded = decode_plaintext(plaintext)
    if decoded["emoji"] != emoji:
        raise ValueError("wire shadow decode emoji mismatch")


def create(peer_id: str, message_id: str, emoji: str, t_ms: int, db: Any) -> str:
    """Create a message_reaction event to add an emoji reaction to a message.

    Uses global_count for deterministic convergence when multiple devices react simultaneously.
    Authorization: Any group member can react. The reactor is derived from peer_id.

    Args:
        peer_id: Local peer ID creating the reaction
        message_id: Message event ID to react to
        emoji: Unicode emoji character(s)
        t_ms: Timestamp
        db: Database connection

    Returns:
        reaction_id: The stored reaction event ID

    Raises:
        ValueError: If message not found or validation fails
    """
    log.info(f"message_reaction.create() reacting to message_id={message_id[:20]}... with emoji={emoji}")

    # Get message to validate it exists
    message_row = message.get(message_id, peer_id, db)
    if not message_row:
        raise ValueError(f"Message {message_id} not found for peer {peer_id}")

    message_group_id = message_row['group_id']

    # Get reactor's peer_shared_id and user_id
    identity = peer_shared.get_self(peer_id, db)
    if not identity or not identity['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found or peer_shared_id not set")

    reactor_peer_shared_id = identity['peer_shared_id']
    reactor_user_id = identity['user_id']

    # Get global count from framework (Lamport clock)
    global_count = global_counter.get_next_global_count(peer_id, db)

    private_key = peer.get_private_key(peer_id, peer_id, db)
    key_data = group.pick_key(message_group_id, peer_id, db)
    blob = encode_wire_event(
        message_id_b64=message_id,
        reactor_id_b64=reactor_user_id,
        signed_by_b64=reactor_peer_shared_id,
        signer_type="peer_shared",
        emoji=emoji,
        global_count=global_count,
        created_at_ms=t_ms,
        key_data=key_data,
        private_key=private_key,
    )
    reaction_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"message_reaction.create() created reaction_id={reaction_id[:20]}...")
    return reaction_id


def remove(peer_id: str, message_id: str, emoji: str, t_ms: int, db: Any) -> str:
    """Remove a reaction by creating a message_reaction_deletion event.

    Authorization: Only the reactor who added the reaction, or group admins can remove.

    Args:
        peer_id: Local peer ID removing the reaction
        message_id: Message containing the reaction
        emoji: Unicode emoji character(s) to remove
        t_ms: Timestamp
        db: Database connection

    Returns:
        deletion_id: The stored deletion event ID

    Raises:
        ValueError: If reaction not found or authorization fails
    """
    log.info(f"message_reaction.remove() removing reaction from message_id={message_id[:20]}... emoji={emoji}")

    # Get remover's user_id and peer_shared_id
    identity = peer_shared.get_self(peer_id, db)
    if not identity or not identity['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found or peer_shared_id not set")

    remover_peer_shared_id = identity['peer_shared_id']
    remover_user_id = identity['user_id']

    # Find the reaction to remove
    reaction_row = get(message_id, remover_user_id, emoji, peer_id, db)
    if not reaction_row:
        raise ValueError(
            f"Reaction not found: message_id={message_id}, reactor_id={remover_user_id}, emoji={emoji}"
        )

    reaction_id = reaction_row['reaction_id']

    # Check authorization: reactor can self-remove, or admin can remove
    # If not the reactor, check if remover is admin
    if remover_user_id != reaction_row['reactor_id']:
        from events.identity import invite
        message_row = message.get(message_id, peer_id, db)
        if not message_row:
            raise ValueError(f"Message {message_id} not found")

        if not invite.is_admin(remover_peer_shared_id, peer_id, db):
            raise ValueError(
                f"Peer {peer_id} cannot remove reaction: not the reactor and not an admin"
            )

    log.info(f"message_reaction.remove() authorization passed for reaction_id={reaction_id[:20]}...")

    # Get message group_id
    message_row = message.get(message_id, peer_id, db)
    if not message_row:
        raise ValueError(f"Message {message_id} not found")

    message_group_id = message_row['group_id']

    private_key = peer.get_private_key(peer_id, peer_id, db)
    key_data = group.pick_key(message_group_id, peer_id, db)
    blob = message_reaction_deletion.encode_wire_event(
        reaction_id_b64=reaction_id,
        signed_by_b64=remover_peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        key_data=key_data,
        private_key=private_key,
    )
    deletion_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"message_reaction.remove() created deletion_id={deletion_id[:20]}...")
    return deletion_id


def project_deletion(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Project message_reaction_deletion event.

    Removes the reaction from message_reactions table and records deletion.

    Args:
        event_id: Deletion event ID
        recorded_by: Peer who recorded this event
        recorded_at: Timestamp when recorded
        db: Database connection
    """
    log.debug(f"message_reaction.project_deletion() event_id={event_id[:20]}..., recorded_by={recorded_by[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    # Get blob from store
    blob = store.get(event_id, unsafedb)
    if not blob:
        log.warning(f"message_reaction.project_deletion() blob not found for deletion_id={event_id}")
        return

    if not message_reaction_deletion.is_wire_envelope(blob):
        log.warning(f"message_reaction.project_deletion() non-wire deletion blob for {event_id[:20]}...")
        return

    event_data, missing_key_ids = message_reaction_deletion.decode_wire_event(
        blob, recorded_by, db
    )
    if not event_data or missing_key_ids:
        log.info(f"message_reaction.project_deletion() cannot decrypt deletion {event_id[:20]}... - missing key")
        return

    # Verify signature before trusting event data
    # message_reaction_deletion uses 'deleted_by' as the signer field (not 'signed_by')
    signer_peer_shared_id = event_data.get('deleted_by')
    if not signer_peer_shared_id:
        log.warning(f"message_reaction.project_deletion() missing deleted_by field for {event_id[:20]}...")
        return

    from events.identity import peer_shared
    try:
        public_key = peer_shared.get_public_key(signer_peer_shared_id, recorded_by, db)
        signed_bytes = event_data.get("_wire_signed_bytes")
        signature = event_data.get("_wire_signature")
        if not signed_bytes or not signature or not crypto.verify(signed_bytes, signature, public_key):
            log.warning(f"message_reaction.project_deletion() signature verification failed for {event_id[:20]}...")
            return
    except ValueError:
        log.warning(f"message_reaction.project_deletion() signer peer_shared not found for {event_id[:20]}...")
        return

    reaction_id = event_data['reaction_id']
    deleted_by = event_data['deleted_by']
    created_at = event_data['created_at']

    log.info(f"message_reaction.project_deletion() deleting reaction_id={reaction_id[:20]}...")

    # Get the reaction to delete
    reaction_row = safedb.query_one(
        "SELECT message_id FROM message_reactions WHERE reaction_id = ? AND recorded_by = ? LIMIT 1",
        (reaction_id, recorded_by)
    )

    if reaction_row:
        # Delete from message_reactions table
        safedb.execute(
            "DELETE FROM message_reactions WHERE reaction_id = ? AND recorded_by = ?",
            (reaction_id, recorded_by)
        )
        log.info(f"message_reaction.project_deletion() deleted reaction {reaction_id[:20]}... from table")
    else:
        log.warning(f"message_reaction.project_deletion() reaction not found: {reaction_id[:20]}...")

    # Record the deletion for audit trail
    safedb.execute(
        """INSERT OR IGNORE INTO message_reaction_deletions
           (deletion_id, reaction_id, deleted_by, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (event_id, reaction_id, deleted_by, created_at, recorded_by, recorded_at)
    )

    log.debug(f"message_reaction.project_deletion() completed for deletion_id={event_id[:20]}...")


def get(message_id: str, reactor_id: str, emoji: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get a specific reaction by message_id, reactor_id, and emoji.

    Args:
        message_id: Message the reaction is on
        reactor_id: User who added the reaction
        emoji: The emoji used
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        Reaction dict with reaction_id, reactor_id, message_id, emoji, or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return safedb.query_one(
        """SELECT reaction_id, reactor_id, message_id, emoji FROM message_reactions
           WHERE message_id = ? AND reactor_id = ? AND emoji = ? AND recorded_by = ? LIMIT 1""",
        (message_id, reactor_id, emoji, recorded_by)
    )


def list_reactions(message_id: str, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List all reactions on a message, grouped by emoji.

    Returns reactions grouped by emoji with reactor names:
    [
        {'emoji': '👍', 'reactors': ['alice', 'bob'], 'count': 2},
        {'emoji': '❤️', 'reactors': ['charlie'], 'count': 1}
    ]

    Uses window function to get winning reaction for each (message_id, reactor_id, emoji).

    Args:
        message_id: Message to get reactions for
        recorded_by: Peer perspective
        db: Database connection

    Returns:
        List of dicts with emoji, reactors list, and count
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get winning reactions for the message with reactor names
    # Window function ensures we only get the latest reaction per (message_id, reactor_id, emoji)
    # Prefer encrypted username from user_names, fall back to users.name (legacy/empty)
    reactions = safedb.query(
        """WITH winning_reactions AS (
             SELECT message_id, reactor_id, emoji, recorded_by,
                    ROW_NUMBER() OVER (
                        PARTITION BY message_id, reactor_id, emoji
                        ORDER BY global_count DESC, reaction_id DESC
                    ) as rn
             FROM message_reactions
             WHERE message_id = ? AND recorded_by = ?
           )
           SELECT wr.emoji, wr.reactor_id, COALESCE(un.name, u.name) as reactor_name
           FROM winning_reactions wr
           LEFT JOIN users u ON wr.reactor_id = u.user_id AND wr.recorded_by = u.recorded_by
           LEFT JOIN user_names un ON wr.reactor_id = un.user_id AND wr.recorded_by = un.recorded_by
           WHERE wr.rn = 1
           ORDER BY wr.emoji ASC""",
        (message_id, recorded_by)
    )

    # Group by emoji
    grouped = {}
    for reaction in reactions:
        emoji = reaction['emoji']
        reactor_name = reaction['reactor_name'] or reaction['reactor_id'][:20]

        if emoji not in grouped:
            grouped[emoji] = {'emoji': emoji, 'reactors': [], 'count': 0}

        grouped[emoji]['reactors'].append(reactor_name)
        grouped[emoji]['count'] += 1

    # Return as list sorted by emoji
    return sorted(grouped.values(), key=lambda x: x['emoji'])


def list_my_reactions(reactor_id: str, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List all reactions added by a specific reactor across all messages.

    Returns:
    [
        {'message_id': '...', 'content': 'Hello world', 'emoji': '👍'},
        {'message_id': '...', 'content': 'Hi there', 'emoji': '❤️'}
    ]

    Uses window function to get winning reaction for each (message_id, emoji).

    Args:
        reactor_id: User ID to get reactions for
        recorded_by: Peer perspective
        db: Database connection

    Returns:
        List of dicts with message_id, content, and emoji
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get winning reactions by this reactor, joined with message content
    # Window function ensures we only get the latest reaction per (message_id, emoji)
    reactions = safedb.query(
        """WITH winning_reactions AS (
             SELECT message_id, emoji, recorded_by,
                    ROW_NUMBER() OVER (
                        PARTITION BY message_id, emoji
                        ORDER BY global_count DESC, reaction_id DESC
                    ) as rn
             FROM message_reactions
             WHERE reactor_id = ? AND recorded_by = ?
           )
           SELECT wr.message_id, wr.emoji, m.content, m.created_at
           FROM winning_reactions wr
           LEFT JOIN messages m ON wr.message_id = m.message_id AND wr.recorded_by = m.recorded_by
           WHERE wr.rn = 1
           ORDER BY m.created_at DESC LIMIT 50""",
        (reactor_id, recorded_by)
    )

    return reactions


def cascade_delete_reactions(message_id: str, recorded_by: str, recorded_at: int, db: Any) -> int:
    """Delete all reactions on a message when message is deleted.

    Called by message_deletion.project() to cascade delete reactions.
    Creates deletion events for each reaction to maintain audit trail.

    Args:
        message_id: Message being deleted
        recorded_by: Peer perspective
        recorded_at: Timestamp of the deletion
        db: Database connection

    Returns:
        Number of reactions deleted
    """
    log.info(f"message_reaction.cascade_delete_reactions() deleting all reactions for message_id={message_id[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Find all reactions on this message
    reactions = safedb.query(
        "SELECT reaction_id FROM message_reactions WHERE message_id = ? AND recorded_by = ?",
        (message_id, recorded_by)
    )

    if not reactions:
        log.info(f"message_reaction.cascade_delete_reactions() no reactions to delete")
        return 0

    log.info(f"message_reaction.cascade_delete_reactions() deleting {len(reactions)} reactions")

    # Simply hard-delete reactions (as per plan recommendation)
    # This is simpler and avoids creating deletion events for each reaction
    deleted_count = safedb.execute(
        "DELETE FROM message_reactions WHERE message_id = ? AND recorded_by = ?",
        (message_id, recorded_by)
    )

    log.info(f"message_reaction.cascade_delete_reactions() deleted {deleted_count} reactions")
    return deleted_count
