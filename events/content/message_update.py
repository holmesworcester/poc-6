"""Message update event type (shareable, encrypted).

Allows message authors to edit the text content of their messages.
Uses global_count for convergent update ordering (highest wins).
"""
from typing import Any
import logging
from core import crypto
from core import store
from events.content import message
from events.identity import peer_shared
from core import global_counter
from core.db import create_safe_db, create_unsafe_db
from core.projection_v2.types import ProjectorResult, WriteOp

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

    # Build update event
    event_data = {
        'type': 'message_update',
        'message_id': message_id,
        'group_id': group_id,
        'edited_by': peer_shared_id,
        'signer_type': 'peer_shared',
        'author_id': user_id,
        'global_count': global_count,
        'new_content': new_content,
        'created_at': t_ms,
    }

    event_id = store.publish(event_data, group_id, peer_id, t_ms, db)

    log.info(
        f"message_update.create() created update_id={event_id} for message_id={message_id}, "
        f"global_count={global_count}"
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
