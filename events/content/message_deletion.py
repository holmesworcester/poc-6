"""Message deletion event type.

Analogous to blocking/unblocking: deletion events act as a permanent block on message projection.
If a deletion exists for a message, the message projection is skipped (like a blocked event).

Forward Secrecy: When messages are deleted, their encryption keys are marked for purging.
Batch rekeying operations move all content to new "clean" keys before old keys are destroyed.
"""
from typing import Any
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


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

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get message to validate it exists and get group_id
    message_row = safedb.query_one(
        "SELECT author_id, group_id, channel_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (message_id, peer_id)
    )
    if not message_row:
        raise ValueError(f"Message {message_id} not found for peer {peer_id}")

    message_group_id = message_row['group_id']

    # Get deleter's peer_shared_id
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_id, peer_id)
    )
    if not peer_self_row or not peer_self_row['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found or peer_shared_id not set")

    deleter_peer_shared_id = peer_self_row['peer_shared_id']

    # Authorization check using shared validate() function
    if not validate(message_id, deleter_peer_shared_id, peer_id, db):
        raise ValueError(
            f"Peer {peer_id} cannot delete message {message_id}: "
            f"not the author and not an admin"
        )

    log.info(f"message_deletion.create() authorization passed")

    # Create deletion event
    event_data = {
        'type': 'message_deletion',
        'message_id': message_id,
        'created_by': deleter_peer_shared_id,
        'created_at': t_ms
    }

    # Sign the event
    from events.identity import peer
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Get group key for encryption (message was in this group, so deletion should be too)
    from events.group import group
    key_data = group.pick_key(message_group_id, peer_id, db)

    # Wrap (canonicalize + encrypt)
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, key_data, db)

    # Store event (no commit - caller owns transaction)
    deletion_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"message_deletion.create() created deletion_id={deletion_id[:20]}...")
    return deletion_id


def project(deletion_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project message_deletion event.

    Analogous to unblocking: when a deletion is projected, it acts like adding a permanent block.
    The message is removed from the messages table, the blob is deleted from store,
    and future message projections are skipped (via deleted_events table).

    For forward secrecy: marks the encryption key for purging so it can be rekeyed
    and later destroyed.

    Args:
        deletion_id: Deletion event ID
        recorded_by: Peer who recorded this event
        recorded_at: When this peer recorded it
        db: Database connection

    Returns:
        deletion_id if successful, None if blocked
    """
    log.info(f"message_deletion.project() deletion_id={deletion_id[:20]}..., recorded_by={recorded_by[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    # Get blob from store
    blob = store.get(deletion_id, unsafedb)
    if not blob:
        log.warning(f"message_deletion.project() blob not found for deletion_id={deletion_id}")
        return None

    # Unwrap (decrypt)
    plaintext, missing_key_ids = crypto.unwrap_event(blob, recorded_by, db)
    if not plaintext or missing_key_ids:
        # Encrypted but we don't have the key yet - will be blocked by recorded.project()
        log.info(f"message_deletion.project() cannot decrypt deletion {deletion_id[:20]}... - missing key")
        return None

    # Parse event
    event_data = crypto.parse_json(plaintext)
    message_id = event_data['message_id']
    deleted_by = event_data['created_by']
    created_at = event_data['created_at']

    log.info(f"message_deletion.project() deleting message_id={message_id[:20]}... deleted_by={deleted_by[:20]}...")

    # Check if message exists for authorization validation
    message_row = safedb.query_one(
        "SELECT author_id, group_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (message_id, recorded_by)
    )

    # If message exists, validate authorization strictly
    if message_row:
        if not validate(message_id, deleted_by, recorded_by, db):
            log.warning(f"message_deletion.project() authorization FAILED: {deleted_by[:20]}... cannot delete message {message_id[:20]}...")
            return None
    else:
        # Message doesn't exist yet - accept deletion as a "pre-block"
        # Authorization will be validated if/when message arrives and tries to project
        log.info(f"message_deletion.project() message not found yet - accepting deletion as pre-block")

    # Insert deletion record (idempotent with PRIMARY KEY on message_id, recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO message_deletions
           (deletion_id, message_id, deleted_by, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (deletion_id, message_id, deleted_by, created_at, recorded_by, recorded_at)
    )

    # Get message blob to extract the key_id it was encrypted with
    message_blob = store.get(message_id, unsafedb)
    if message_blob:
        try:
            # Extract key_id from blob (first 16 bytes)
            key_id_bytes = message_blob[:crypto.ID_SIZE]
            key_id_b64 = crypto.b64encode(key_id_bytes)

            # Mark this key for purging (for forward secrecy)
            safedb.execute(
                """INSERT OR IGNORE INTO keys_to_purge (key_id, marked_at, recorded_by)
                   VALUES (?, ?, ?)""",
                (key_id_b64, recorded_at, recorded_by)
            )
            log.info(f"message_deletion.project() marked key {key_id_b64[:20]}... for purging (forward secrecy)")
        except Exception as e:
            log.warning(f"message_deletion.project() failed to mark key for purging: {e}")
            # Continue anyway - forward secrecy is best-effort

    # Delete the message if it exists (analogous to unblocking - but we remove instead of project)
    safedb.execute(
        "DELETE FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, recorded_by)
    )
    log.info(f"message_deletion.project() deleted message {message_id[:20]}... from messages table (may have already been deleted or not yet arrived)")

    # Mark message as deleted in deleted_events table to prevent future projection
    safedb.execute(
        """INSERT OR IGNORE INTO deleted_events (event_id, recorded_by, deleted_at)
           VALUES (?, ?, ?)""",
        (message_id, recorded_by, recorded_at)
    )
    log.info(f"message_deletion.project() marked message {message_id[:20]}... as deleted in deleted_events")

    # Cascade delete from valid_events to ensure convergence
    deleted_count = _cascade_delete_from_valid_events(message_id, recorded_by, safedb)
    log.info(f"message_deletion.project() cascaded deletion of {deleted_count} events from valid_events (message + dependents)")

    # Remove from shareable_events if it was marked shareable
    safedb.execute(
        "DELETE FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
        (message_id, recorded_by)
    )

    # Delete blob from store to clean up storage
    unsafedb.execute(
        "DELETE FROM store WHERE id = ?",
        (message_id,)
    )
    log.info(f"message_deletion.project() deleted message blob {message_id[:20]}... from store")

    # Return deletion_id to mark as valid
    return deletion_id


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
        all_messages = safedb.query(
            """SELECT message_id FROM messages
               WHERE recorded_by = ?
               AND message_id NOT IN (SELECT event_id FROM deleted_events WHERE recorded_by = ?)""",
            (peer_id, peer_id)
        )

        messages_using_purge_key = []
        for msg_row in all_messages:
            message_id = msg_row['message_id']
            blob = unsafedb.query_one(
                "SELECT blob FROM store WHERE id = ?",
                (message_id,)
            )
            if not blob:
                continue

            # Extract key_id hint from blob (first 16 bytes, then base64 encode)
            try:
                key_id_bytes = blob['blob'][:crypto.ID_SIZE]
                key_id_b64 = crypto.b64encode(key_id_bytes)
                if key_id_b64 == purge_key_id:
                    messages_using_purge_key.append(message_id)
            except Exception as e:
                log.warning(f"message_deletion.run_message_purge_cycle() error checking message {message_id[:20]}...: {e}")
                continue

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
                # Immediately project the rekey
                message_rekey.project(rekey_id, peer_id, t_ms, db)
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

    log.info(f"message_deletion.run_message_purge_cycle() complete: {stats['messages_rekeyed']} messages rekeyed, {stats['keys_purged']} keys purged")
    return stats
