"""Message update event type (shareable, encrypted).

Allows message authors to edit the text content of their messages.
Uses global_count for convergent update ordering (highest wins).
"""
from typing import Any
import logging
import crypto
import store
from events.group import group as group_module
from events.identity import peer
from events._shared import updates
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)

# Event type registration
EVENT_TYPE = 'message_update'
SHAREABLE = True
EPHEMERAL = False
PROJECTION_TABLE = ('message_updates', 'update_id')


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
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get the original message to verify ownership and get group_id
    message_row = safedb.query_one(
        """SELECT message_id, group_id, author_id, content
           FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1""",
        (message_id, peer_id)
    )
    if not message_row:
        raise ValueError(f"Message {message_id} not found")

    group_id = message_row['group_id']
    original_author_id = message_row['author_id']

    # Get our identity from peer_self
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id, user_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_id, peer_id)
    )
    if not peer_self_row or not peer_self_row['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found in peer_self table")
    if not peer_self_row['user_id']:
        raise ValueError(f"User identity not set for peer {peer_id}")

    peer_shared_id = peer_self_row['peer_shared_id']
    user_id = peer_self_row['user_id']

    # Check authorization - only the original author can edit
    if user_id != original_author_id:
        raise ValueError("Only the message author can edit their messages")

    # Validate new content
    if not new_content.strip():
        raise ValueError("Message content cannot be empty")

    # Get global count from framework (Lamport clock)
    global_count = updates.get_next_global_count(peer_id, db)

    # Build update event
    event_data = {
        'type': 'message_update',
        'message_id': message_id,
        'group_id': group_id,
        'edited_by': peer_shared_id,
        'author_id': user_id,
        'global_count': global_count,
        'new_content': new_content,
        'created_at': t_ms,
    }

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Get key_data for encryption
    key_data = group_module.pick_key(group_id, peer_id, db)

    # Wrap (canonicalize + encrypt)
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, key_data, db)

    # Store event
    event_id = store.event(blob, peer_id, t_ms, db)

    log.info(
        f"message_update.create() created update_id={event_id} for message_id={message_id}, "
        f"global_count={global_count}"
    )

    return event_id


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project a message update event into the database.

    Stores all updates unconditionally (INSERT OR IGNORE). Winners are determined
    at query time via window functions (highest global_count, then highest update_id).

    Note: Does NOT apply updates to the messages table. Message content is determined
    at query time via the get() function.

    Args:
        event_id: Event ID of the message_update
        recorded_by: Peer perspective
        recorded_at: Timestamp when recorded
        db: Database connection

    Returns:
        event_id if successfully projected, None otherwise
    """
    log.debug(f"message_update.project() projecting update_id={event_id}, seen_by={recorded_by}")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get and unwrap event
    event_blob = store.get(event_id, unsafedb)
    if not event_blob:
        log.warning(f"message_update.project() blob not found for update_id={event_id}")
        return None

    unwrapped, _ = crypto.unwrap(event_blob, recorded_by, db)
    if not unwrapped:
        log.warning(f"message_update.project() unwrap failed for update_id={event_id}")
        return None

    event_data = crypto.parse_json(unwrapped)

    # Verify signature
    from events.identity import peer_shared
    edited_by = event_data.get('edited_by')
    public_key = peer_shared.get_public_key(edited_by, recorded_by, db)

    if not crypto.verify_event(event_data, public_key):
        log.warning(f"message_update.project() signature verification failed for update_id={event_id}")
        return None

    # Extract fields
    update_id = event_id
    message_id = event_data.get('message_id')
    group_id = event_data.get('group_id')
    author_id = event_data.get('author_id')
    global_count = event_data.get('global_count')
    new_content = event_data.get('new_content')
    created_at = event_data.get('created_at')

    # Check that original message exists
    message_row = safedb.query_one(
        "SELECT author_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (message_id, recorded_by)
    )
    if not message_row:
        log.warning(f"message_update.project() original message not found for message_id={message_id}")
        return None

    # Check authorization - author_id must match original message author
    if author_id != message_row['author_id']:
        log.warning(f"message_update.project() author mismatch: {author_id} != {message_row['author_id']}")
        return None

    # Validate content
    if not new_content or not new_content.strip():
        log.warning(f"message_update.project() empty content in update_id={update_id}")
        return None

    # Store the update unconditionally (INSERT OR IGNORE ensures idempotency)
    # Winner determination happens at query time via get() function
    safedb.execute(
        """INSERT OR IGNORE INTO message_updates
           (update_id, message_id, group_id, edited_by, author_id, global_count,
            new_content, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (update_id, message_id, group_id, edited_by, author_id, global_count,
         new_content, created_at, recorded_by, recorded_at)
    )

    log.debug(f"message_update.project() stored update_id={update_id[:20]}... for message_id={message_id[:20]}...")

    # Record as valid event
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (update_id, recorded_by)
    )

    return event_id


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
    result = updates.get_winners(
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
