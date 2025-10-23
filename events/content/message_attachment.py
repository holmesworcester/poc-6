"""Message attachment event type - links messages to files.

Attachments ARE group-wrapped (access control).
"""
from typing import Any
import logging
import crypto
import store
from events.group import group
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(peer_id: str, message_id: str, file_id: str,
           filename: str | None, mime_type: str | None,
           t_ms: int, db: Any) -> str:
    """Create message_attachment event linking message to file.

    This event IS group-encrypted (access control).

    Args:
        peer_id: Local peer creating this event
        message_id: Message being attached to
        file_id: File being attached
        filename: Optional filename
        mime_type: Optional MIME type
        t_ms: Timestamp
        db: Database connection

    Returns:
        attachment_event_id
    """
    log.info(f"message_attachment.create() message_id={message_id[:20]}..., "
             f"file_id={file_id[:20]}...")

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get message to verify access and get group_id
    message_row = safedb.query_one(
        "SELECT group_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (message_id, peer_id)
    )
    if not message_row:
        raise ValueError(f"Message {message_id} not found for peer {peer_id}")

    group_id = message_row['group_id']

    # Get peer_shared_id for created_by field
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_id, peer_id)
    )
    if not peer_self_row or not peer_self_row['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found or peer_shared_id not set")

    peer_shared_id = peer_self_row['peer_shared_id']

    # Build event structure
    event_data = {
        'type': 'message_attachment',
        'message_id': message_id,
        'file_id': file_id,
        'filename': filename,
        'mime_type': mime_type,
        'created_by': peer_shared_id,
        'created_at': t_ms
    }

    # Sign the event
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Get group key for encryption
    key_data = group.pick_key(group_id, peer_id, db)

    # Wrap (canonicalize + group encrypt)
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, key_data, db)

    # Store event
    attachment_event_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"message_attachment.create() created attachment_event_id={attachment_event_id[:20]}...")
    return attachment_event_id


def project(event_id: str, event_data: dict[str, Any], recorded_by: str,
            recorded_at: int, db: Any) -> None:
    """Project message_attachment event into message_attachments table.

    VALIDATION: Only the message creator can attach files to their message.

    Args:
        event_id: Event ID
        event_data: Decrypted/unwrapped event data
        recorded_by: Peer who recorded this event
        recorded_at: Timestamp when recorded
        db: Database connection
    """
    log.debug(f"message_attachment.project() event_id={event_id[:20]}..., "
              f"recorded_by={recorded_by[:20]}...")

    message_id = event_data.get('message_id')
    file_id = event_data.get('file_id')
    filename = event_data.get('filename')
    mime_type = event_data.get('mime_type')
    created_by = event_data.get('created_by')

    if not all([message_id, file_id, created_by]):
        log.warning(f"message_attachment.project() missing required fields: {list(event_data.keys())}")
        return

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Validate: attachment creator must match message creator
    # NOTE: message_id is a dependency, so the message should already be projected
    message_row = safedb.query_one(
        "SELECT author_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (message_id, recorded_by)
    )

    if not message_row:
        log.error(f"message_attachment.project() BUG: message not found (should be blocked by deps): {message_id[:20]}...")
        return

    if message_row['author_id'] != created_by:
        log.warning(f"message_attachment.project() VALIDATION FAILED: attachment created_by={created_by[:20]}... "
                   f"does not match message author_id={message_row['author_id'][:20]}...")
        return

    # Insert or ignore
    safedb.execute(
        """INSERT OR IGNORE INTO message_attachments
           (message_id, file_id, filename, mime_type, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (message_id, file_id, filename, mime_type, recorded_by, recorded_at)
    )

    log.debug(f"message_attachment.project() projected attachment "
              f"message={message_id[:20]}... file={file_id[:20]}...")
