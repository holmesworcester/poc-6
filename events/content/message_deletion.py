"""Message deletion event type.

Analogous to blocking/unblocking: deletion events act as a permanent block on message projection.
If a deletion exists for a message, the message projection is skipped (like a blocked event).

Forward Secrecy: When messages are deleted, their encryption keys are marked for purging.
Batch rekeying operations move all content to new "clean" keys before old keys are destroyed.
"""

# Registry metadata
EVENT_TYPE = 'message_deletion'
SHAREABLE = True  # Deletions sync to remove messages from all peers
PROJECTION_TABLE = ('message_deletions', 'deletion_id')

# Wire format constants
WIRE_TYPE_CODE = 0x04  # TYPE_MESSAGE_DELETION
WIRE_PLAINTEXT_SIZE = 344  # MESSAGE_DELETION_PLAINTEXT_SIZE

from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from core.projection.types import ProjectorResult, WriteOp, Command
from core.projection.apply import register_command_handler, cascade_delete_from_valid_events
from events.content import message
from events.identity import peer_shared, peer
from events.group import group as group_module
from core.db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for message_deletion event type

def encode_plaintext(message_id: bytes) -> bytes:
    """Encode a message_deletion payload plaintext (pre-encryption)."""
    wire_format._require_len("message_id", message_id, 16)
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = message_id
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a message_deletion payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"message_deletion plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {"message_id": data[0:16]}


def _encrypt_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    """Encrypt message_deletion plaintext into wire payload."""
    if len(plaintext) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"message_deletion plaintext must be {WIRE_PLAINTEXT_SIZE} bytes")
    key_id = wire_format._require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("message_deletion payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != WIRE_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for message_deletion payload")
    payload = key_id + nonce + ciphertext
    return wire_format._require_len("payload", payload, wire_format.PAYLOAD_SIZE)


def _decrypt_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    """Decrypt wire payload to message_deletion plaintext."""
    if key_data.get("type") != "symmetric":
        raise ValueError("message_deletion payload requires symmetric key")
    key_id = payload[:16]
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a message_deletion wire envelope."""
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
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    """Encode a complete message_deletion wire event."""
    message_id = crypto.b64decode(message_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)

    plaintext = encode_plaintext(message_id=message_id)
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
    """Decode a message_deletion wire event."""
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
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }
    return event_data, []


# v2 event specification
EVENT_SPEC = {
    'encrypted': True,  # Group-wrapped via store.publish
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {},  # No strict requirements - can be a pre-block for message
    'optional': {
        'message': {
            'source': 'table',
            'table': 'messages',
            'key': 'message_id',
            'fields': ['message_id', 'author_id', 'group_id'],
        },
    },
    'cascade_on_delete': [],  # This event deletes others, not cascaded itself
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for message_deletion events.

    Validates authorization, then returns writes to delete the message
    and commands for cascade deletion and blob removal.
    """
    event_data = ctx.event_data

    message_id = event_data.get('message_id')
    deleted_by = event_data.get('signed_by')
    created_at = event_data.get('created_at')

    if not all([message_id, deleted_by, created_at is not None]):
        log.warning(f"message_deletion.project_pure() missing required fields")
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_message_deletion(message_id)

    signer = ctx.signer or {}
    signer_user_id = signer.get('user_id')
    signer_is_admin = signer.get('is_admin')

    # Get message from deps (optional - may be a pre-block)
    message_row = ctx.deps.get('message')
    is_author = False
    if message_row and signer_user_id:
        message_author_id = message_row.get('author_id')
        is_author = (signer_user_id == message_author_id)

    if message_row:
        if not is_author and not signer_is_admin:
            return ProjectorResult(writes=tuple(), valid_event=False)

        writes = [
            WriteOp(
                op='insert',
                table='message_deletions',
                values={
                    'deletion_id': ctx.event_id,
                    'message_id': message_id,
                    'deleted_by': deleted_by,
                    'created_at': created_at,
                    'recorded_by': ctx.recorded_by,
                    'recorded_at': ctx.recorded_at,
                },
            ),
            WriteOp(
                op='insert',
                table='deleted_events',
                values={
                    'event_id': message_id,
                    'recorded_by': ctx.recorded_by,
                    'deleted_at': ctx.recorded_at,
                },
            ),
            WriteOp(
                op='delete',
                table='messages',
                values={},
                where={
                    'message_id': message_id,
                    'recorded_by': ctx.recorded_by,
                },
            ),
            WriteOp(
                op='delete',
                table='shareable_events',
                values={},
                where={
                    'event_id': message_id,
                    'can_share_peer_id': ctx.recorded_by,
                },
            ),
            WriteOp(
                op='delete',
                table='pending_message_deletions',
                values={},
                where={
                    'message_id': message_id,
                    'recorded_by': ctx.recorded_by,
                },
            ),
        ]

        # Command to handle cascade delete, blob removal, and key marking
        commands = (
            Command(
                command_type='finalize_message_deletion',
                args={
                    'message_id': message_id,
                    'deleted_by': deleted_by,
                    'is_author': is_author,
                    'has_message': True,
                }
            ),
        )

        return ProjectorResult(writes=tuple(writes), valid_event=True, commands=commands)

    # Message not projected yet: record pending deletion for future validation
    writes = [
        WriteOp(
            op='insert',
            table='pending_message_deletions',
            values={
                'deletion_id': ctx.event_id,
                'message_id': message_id,
                'deleted_by': deleted_by,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    ]

    return ProjectorResult(writes=tuple(writes), valid_event=True)


def _handle_finalize_message_deletion(args: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Handle message deletion finalization.

    - Validates authorization if message exists and deleter is not author
    - Marks encryption key for purging (forward secrecy)
    - Cascades deletion from valid_events
    - Deletes blob from store
    """
    message_id = args['message_id']
    deleted_by = args['deleted_by']
    is_author = args['is_author']
    has_message = args['has_message']

    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    # If message exists and deleter is not author, check admin status
    if has_message and not is_author:
        from events.identity import invite
        if not invite.is_admin(deleted_by, recorded_by, db):
            log.warning(f"message_deletion: {deleted_by[:20]}... is not author or admin, skipping finalization")
            # Note: writes already happened, but this is a validation failure
            # In a stricter system we'd rollback, but for now we log and continue
            return

    # Mark encryption key for purging (forward secrecy)
    message_blob = store.get(message_id, unsafedb)
    if message_blob:
        try:
            if not message.is_wire_envelope(message_blob):
                log.warning("message_deletion: non-wire message blob, skipping key purge mark")
                return
            _header, payload, _signature = wire_format.parse_envelope(message_blob)
            key_id_bytes = payload[:crypto.KEY_ID_SIZE]
            key_id_b64 = crypto.b64encode(key_id_bytes)
            safedb.execute(
                """INSERT OR IGNORE INTO keys_to_purge (key_id, marked_at, recorded_by)
                   VALUES (?, ?, ?)""",
                (key_id_b64, recorded_at, recorded_by)
            )
            log.info(f"message_deletion: marked key {key_id_b64[:20]}... for purging")
        except Exception as e:
            log.warning(f"message_deletion: failed to mark key for purging: {e}")

    # Cascade delete from valid_events
    deleted_count = cascade_delete_from_valid_events(message_id, recorded_by, db)
    log.info(f"message_deletion: cascaded deletion of {deleted_count} events from valid_events")

    # Remove from negentropy sync tables (keep buckets consistent)
    from events.network import negentropy
    removed = negentropy.remove_events_from_sync_batch(db, recorded_by, [message_id])
    if removed:
        log.info(f"message_deletion: removed {removed} negentropy event(s) for {message_id[:20]}...")

    # Delete blob from store
    unsafedb.execute("DELETE FROM store WHERE id = ?", (message_id,))
    log.info(f"message_deletion: deleted blob {message_id[:20]}... from store")


# Register the command handler at module load time
register_command_handler('finalize_message_deletion', _handle_finalize_message_deletion)


def _cascade_delete_from_valid_events(
    event_id: str,
    recorded_by: str,
    safedb: Any,
    _visited: set = None
) -> int:
    """Recursively delete event and all dependents from valid_events.

    When an event is deleted, this ensures all dependent events are also removed
    from valid_events to maintain convergence. Regardless of event ordering,
    the final valid_events table will be the same.

    Args:
        event_id: The event being deleted
        recorded_by: Peer scope (SafeDB is already scoped to this peer)
        safedb: SafeDB instance
        _visited: Internal cycle detection set (prevents infinite recursion)

    Returns:
        Total number of events deleted from valid_events (including recursively deleted dependents)
    """
    if _visited is None:
        _visited = set()

    # Cycle detection
    if event_id in _visited:
        return 0

    _visited.add(event_id)
    deleted_count = 0

    # Find all children (events that depend on this one)
    children = safedb.query(
        """SELECT DISTINCT child_event_id
           FROM event_dependencies
           WHERE parent_event_id = ? AND recorded_by = ?""",
        (event_id, recorded_by)
    )

    # Recursively delete children first (depth-first traversal)
    for child in children:
        deleted_count += _cascade_delete_from_valid_events(
            child['child_event_id'],
            recorded_by,
            safedb,
            _visited
        )

    # Delete this event from valid_events
    safedb.execute(
        "DELETE FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (event_id, recorded_by)
    )

    log.debug(f"_cascade_delete_from_valid_events() deleted {event_id[:20]}... and {deleted_count} dependents for peer {recorded_by[:20]}...")

    return deleted_count + 1


def validate(message_id: str, deleted_by: str, recorded_by: str, db: Any) -> bool:
    """Validate that deleted_by has authorization to delete the message.

    Authorization rules:
    1. deleted_by is the message author (self-deletion), OR
    2. deleted_by is an admin in the network

    Args:
        message_id: Message event ID to check
        deleted_by: peer_shared_id attempting deletion
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        True if authorized, False otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get message to check authorship (if it exists)
    message_row = safedb.query_one(
        "SELECT author_id, group_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (message_id, recorded_by)
    )

    # Check if deleted_by is the message author (only if message exists)
    if message_row:
        message_author_id = message_row['author_id']
        if deleted_by == message_author_id:
            return True

    # If not author (or message doesn't exist yet), check if deleted_by is an admin
    # Use centralized is_admin() function which handles both normal admins and first_peer
    from events.identity import invite
    return invite.is_admin(deleted_by, recorded_by, db)


def create(peer_id: str, message_id: str, t_ms: int, db: Any) -> str:
    """Create a message_deletion event to delete a message.

    Validates that the deleter is either:
    1. The message author (self-deletion), OR
    2. An admin in the message's group (admin deletion)

    Args:
        peer_id: Local peer ID creating the deletion
        message_id: Message event ID to delete
        t_ms: Timestamp
        db: Database connection

    Returns:
        deletion_id: The stored deletion event ID

    Raises:
        ValueError: If message not found or deleter lacks permission
    """
    log.info(f"message_deletion.create() deleting message_id={message_id[:20]}... by peer={peer_id[:20]}...")

    # Get message to validate it exists and get group_id
    message_row = message.get(message_id, peer_id, db)
    if not message_row:
        raise ValueError(f"Message {message_id} not found for peer {peer_id}")

    message_group_id = message_row['group_id']

    # Get deleter's peer_shared_id
    identity = peer_shared.get_self(peer_id, db)
    if not identity or not identity['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found or peer_shared_id not set")

    deleter_peer_shared_id = identity['peer_shared_id']

    # Authorization check using shared validate() function
    if not validate(message_id, deleter_peer_shared_id, peer_id, db):
        raise ValueError(
            f"Peer {peer_id} cannot delete message {message_id}: "
            f"not the author and not an admin"
        )

    log.info(f"message_deletion.create() authorization passed")

    _wire_shadow_message_deletion(message_id)

    private_key = peer.get_private_key(peer_id, peer_id, db)
    key_data = group_module.pick_key(message_group_id, peer_id, db)
    blob = encode_wire_event(
        message_id_b64=message_id,
        signed_by_b64=deleter_peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        key_data=key_data,
        private_key=private_key,
    )
    deletion_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"message_deletion.create() created deletion_id={deletion_id[:20]}...")
    return deletion_id


def _wire_shadow_message_deletion(message_id: str) -> None:
    """Validate message_deletion fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        message_id=crypto.b64decode(message_id),
    )
    decoded = decode_plaintext(plaintext)
    if decoded["message_id"] != crypto.b64decode(message_id):
        raise ValueError("wire shadow decode message_id mismatch")


def run_message_purge_cycle(peer_id: str, t_ms: int, db: Any) -> dict[str, Any]:
    """Execute forward secrecy purge cycle for messages with deleted content.

    After messages are deleted, their encryption keys are marked for purging.
    This batch operation:
    1. Finds all messages encrypted with keys in keys_to_purge
    2. Re-encrypts each message with a new "clean" key using message_rekey events
    3. Deletes old keys from group_keys table
    4. Clears keys_to_purge entries

    Args:
        peer_id: Local peer ID running the purge
        t_ms: Timestamp
        db: Database connection

    Returns:
        Dict with stats: {
            'messages_rekeyed': int,
            'keys_purged': int,
            'errors': list[str]
        }
    """
    log.info(f"message_deletion.run_message_purge_cycle() starting for peer={peer_id[:20]}...")

    safedb = create_safe_db(db, recorded_by=peer_id)
    unsafedb = create_unsafe_db(db)

    stats = {
        'messages_rekeyed': 0,
        'keys_purged': 0,
        'errors': []
    }

    # Find all keys marked for purging
    purge_keys = safedb.query(
        "SELECT key_id FROM keys_to_purge WHERE recorded_by = ? ORDER BY marked_at ASC",
        (peer_id,)
    )

    if not purge_keys:
        log.info(f"message_deletion.run_message_purge_cycle() no keys marked for purging")
        return stats

    log.info(f"message_deletion.run_message_purge_cycle() found {len(purge_keys)} keys to purge")

    # Import here to avoid circular dependency (message_rekey imports message_deletion)
    from events.content import message_rekey
    from events.group import group_key

    # For each key marked for purging, rekey all messages that used it
    for purge_key_row in purge_keys:
        purge_key_id = purge_key_row['key_id']
        log.info(f"message_deletion.run_message_purge_cycle() processing key_id={purge_key_id[:20]}...")

        # Find all messages encrypted with this key
        # Use key_id column for efficient O(1) lookup instead of O(n) blob scanning
        messages_using_purge_key_rows = safedb.query(
            """SELECT message_id FROM messages
               WHERE recorded_by = ? AND key_id = ?
               AND message_id NOT IN (SELECT event_id FROM deleted_events WHERE recorded_by = ?)""",
            (peer_id, purge_key_id, peer_id)
        )
        messages_using_purge_key = [row['message_id'] for row in messages_using_purge_key_rows]

        if not messages_using_purge_key:
            log.info(f"message_deletion.run_message_purge_cycle() no messages found using key {purge_key_id[:20]}...")
            # Still purge the key even if no messages use it
            safedb.execute(
                "DELETE FROM group_keys WHERE key_id = ? AND recorded_by = ?",
                (purge_key_id, peer_id)
            )
            safedb.execute(
                "DELETE FROM keys_to_purge WHERE key_id = ? AND recorded_by = ?",
                (purge_key_id, peer_id)
            )
            stats['keys_purged'] += 1
            continue

        log.info(f"message_deletion.run_message_purge_cycle() found {len(messages_using_purge_key)} messages using key {purge_key_id[:20]}...")

        # Get a clean key to rekey these messages with
        try:
            # Need group_id - extract from one of the messages
            msg_row = safedb.query_one(
                "SELECT group_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
                (messages_using_purge_key[0], peer_id)
            )
            if not msg_row:
                error = f"Could not find group_id for message {messages_using_purge_key[0][:20]}..."
                log.warning(f"message_deletion.run_message_purge_cycle() {error}")
                stats['errors'].append(error)
                continue

            group_id = msg_row['group_id']
            clean_key_id = group_key.get_or_create_clean_key(group_id, peer_id, t_ms, db)
            log.info(f"message_deletion.run_message_purge_cycle() using clean key {clean_key_id[:20]}... for rekeying")
        except Exception as e:
            error = f"Failed to get clean key: {e}"
            log.error(f"message_deletion.run_message_purge_cycle() {error}")
            stats['errors'].append(error)
            continue

        # Rekey each message
        for message_id in messages_using_purge_key:
            try:
                rekey_id = message_rekey.create(message_id, clean_key_id, peer_id, t_ms, db)
                # Projection happens automatically via store.event() in create()
                log.info(f"message_deletion.run_message_purge_cycle() rekeyed message {message_id[:20]}...")
                stats['messages_rekeyed'] += 1
            except Exception as e:
                error = f"Failed to rekey message {message_id[:20]}...: {e}"
                log.warning(f"message_deletion.run_message_purge_cycle() {error}")
                stats['errors'].append(error)
                continue

        # Purge the old key
        safedb.execute(
            "DELETE FROM group_keys WHERE key_id = ? AND recorded_by = ?",
            (purge_key_id, peer_id)
        )
        safedb.execute(
            "DELETE FROM keys_to_purge WHERE key_id = ? AND recorded_by = ?",
            (purge_key_id, peer_id)
        )
        log.info(f"message_deletion.run_message_purge_cycle() purged key {purge_key_id[:20]}...")
        stats['keys_purged'] += 1

    # After purging group_keys, check for prekeys that can be purged
    # A prekey can be purged if ALL group_keys shared via it are now purged
    prekeys_purged = []

    # Find prekeys where all their group_keys are gone
    orphaned_prekeys = safedb.query(
        """SELECT DISTINCT gks.recipient_prekey_id as prekey_id
           FROM group_keys_shared gks
           WHERE gks.recorded_by = ?
           AND NOT EXISTS (
               SELECT 1 FROM group_keys gk
               WHERE gk.key_id = gks.original_key_id AND gk.recorded_by = ?
           )
           AND NOT EXISTS (
               SELECT 1 FROM group_keys_shared gks2
               JOIN group_keys gk ON gks2.original_key_id = gk.key_id AND gk.recorded_by = gks2.recorded_by
               WHERE gks2.recipient_prekey_id = gks.recipient_prekey_id AND gks2.recorded_by = ?
           )""",
        (peer_id, peer_id, peer_id)
    )

    for row in orphaned_prekeys:
        prekey_id = row['prekey_id']
        # Delete private key from group_prekeys
        safedb.execute(
            "DELETE FROM group_prekeys WHERE prekey_id = ? AND recorded_by = ?",
            (prekey_id, peer_id)
        )
        prekeys_purged.append(prekey_id)
        log.info(f"message_deletion.run_message_purge_cycle() purged prekey {prekey_id[:20]}...")

    stats['prekeys_purged'] = len(prekeys_purged)
    log.info(f"message_deletion.run_message_purge_cycle() complete: {stats['messages_rekeyed']} messages rekeyed, {stats['keys_purged']} keys purged, {stats['prekeys_purged']} prekeys purged")
    return stats


def run_message_purge_cycle_for_all_peers(t_ms: int, db: Any) -> dict[str, Any]:
    """Run message purge cycle for all local peers in the database.

    This is the recurring job that should be called periodically to perform
    forward secrecy rekeying and key purging. It:
    1. Gets all local peers
    2. For each peer, runs run_message_purge_cycle to rekey and purge

    Args:
        t_ms: Current time in milliseconds
        db: Database connection (caller must commit)

    Returns:
        Dict with aggregated stats: {
            'peers_processed': int,
            'total_messages_rekeyed': int,
            'total_keys_purged': int,
            'errors': list[str]
        }

    Error Handling:
        - If an error occurs for one peer, it is logged and added to the
          'errors' list, but processing continues for other peers
        - Each peer's changes are committed by the caller, so partial failures
          leave some peers purged and others unchanged
        - Per-message errors during rekeying are collected in the peer's stats
          and aggregated into the total errors list
    """
    log.info(f"message_deletion.run_message_purge_cycle_for_all_peers() t_ms={t_ms}")

    unsafedb = create_unsafe_db(db)

    stats = {
        'peers_processed': 0,
        'total_messages_rekeyed': 0,
        'total_keys_purged': 0,
        'errors': []
    }

    # Get all local peers
    local_peer_rows = unsafedb.query("SELECT peer_id FROM local_peers")

    if not local_peer_rows:
        log.info(f"message_deletion.run_message_purge_cycle_for_all_peers() no local peers found")
        return stats

    log.info(f"message_deletion.run_message_purge_cycle_for_all_peers() found {len(local_peer_rows)} local peers")

    for peer_row in local_peer_rows:
        peer_id = peer_row['peer_id']
        try:
            # Run purge cycle for this peer
            peer_stats = run_message_purge_cycle(peer_id, t_ms, db)

            stats['peers_processed'] += 1
            stats['total_messages_rekeyed'] += peer_stats['messages_rekeyed']
            stats['total_keys_purged'] += peer_stats['keys_purged']
            stats['errors'].extend(peer_stats['errors'])

            log.info(f"message_deletion.run_message_purge_cycle_for_all_peers() peer {peer_id[:20]}...: {peer_stats['messages_rekeyed']} rekeyed, {peer_stats['keys_purged']} purged")

        except Exception as e:
            error = f"Error processing peer {peer_id[:20]}...: {e}"
            log.error(f"message_deletion.run_message_purge_cycle_for_all_peers() {error}")
            stats['errors'].append(error)
            continue

    log.info(f"message_deletion.run_message_purge_cycle_for_all_peers() complete: {stats['peers_processed']} peers, {stats['total_messages_rekeyed']} messages rekeyed, {stats['total_keys_purged']} keys purged")
    return stats
