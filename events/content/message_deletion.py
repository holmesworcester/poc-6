"""Message deletion event type.

Analogous to blocking/unblocking: deletion events act as a permanent block on message projection.
If a deletion exists for a message, the message projection is skipped (like a blocked event).
"""
from typing import Any
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


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

    # Get message to check authorship
    message_row = safedb.query_one(
        "SELECT author_id, group_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (message_id, recorded_by)
    )
    if not message_row:
        # Message not found - not authorized
        return False

    message_author_id = message_row['author_id']

    # Check if deleted_by is the message author
    if deleted_by == message_author_id:
        return True

    # If not author, check if deleted_by is an admin
    # Get deleter's user_id
    deleter_user_row = safedb.query_one(
        "SELECT user_id FROM users WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (deleted_by, recorded_by)
    )
    if not deleter_user_row:
        return False

    deleter_user_id = deleter_user_row['user_id']

    # Get network's admin group ID
    network_row = safedb.query_one(
        "SELECT admins_group_id FROM networks WHERE recorded_by = ? LIMIT 1",
        (recorded_by,)
    )
    if not network_row or not network_row['admins_group_id']:
        return False

    admins_group_id = network_row['admins_group_id']

    # Check if deleter is in admin group
    admin_check = safedb.query_one(
        "SELECT 1 FROM group_members_wip WHERE group_id = ? AND user_id = ? AND recorded_by = ? LIMIT 1",
        (admins_group_id, deleter_user_id, recorded_by)
    )

    return admin_check is not None


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
    The message is removed from the messages table, and future message projections are skipped.

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

    # Authorization check using shared validate() function
    if not validate(message_id, deleted_by, recorded_by, db):
        log.warning(f"message_deletion.project() authorization FAILED: {deleted_by[:20]}... cannot delete message {message_id[:20]}...")
        return None

    # Insert deletion record (idempotent with PRIMARY KEY on message_id, recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO message_deletions
           (deletion_id, message_id, deleted_by, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (deletion_id, message_id, deleted_by, created_at, recorded_by, recorded_at)
    )

    # Delete the message if it exists (analogous to unblocking - but we remove instead of project)
    safedb.execute(
        "DELETE FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, recorded_by)
    )
    log.info(f"message_deletion.project() attempted to delete message {message_id[:20]}... (may have already been deleted or not yet arrived)")

    # Return deletion_id to mark as valid
    return deletion_id
