"""Message event type."""
from __future__ import annotations

# Registry metadata
EVENT_TYPE = 'message'
SHAREABLE = True  # Messages sync to channel members
EPHEMERAL = False
PROJECTION_TABLE = ('messages', 'message_id')

from typing import Any
import logging
from core import crypto
from core import store
from events.identity import peer_shared, user
from events.content import channel
from core.db import create_safe_db, create_unsafe_db
from core.projection_v2.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)

# Default message TTL: 1 week (in milliseconds)
DEFAULT_MESSAGE_TTL_MS = 7 * 24 * 60 * 60 * 1000


# v2 event specification - signed by peer_shared, encrypted
EVENT_SPEC = {
    'encrypted': True,
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

    key_id_bytes = event_blob[:crypto.ID_SIZE]
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

    # Query channel to get group_id and disappearing_time_ms
    channel_row = channel.get(channel_id, peer_id, db)
    if not channel_row:
        raise ValueError(f"Channel {channel_id} not found for peer {peer_id}")

    group_id = channel_row['group_id']
    disappearing_time_ms = channel_row.get('disappearing_time_ms', 0) or 0

    # Look up our identity from peer_self (set when we created/linked our account)
    # This is the canonical source for "what user am I?" - single lookup, no fallback chain
    identity = peer_shared.get_self(peer_id, db)
    if not identity or not identity['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found in peer_self table")
    if not identity['user_id']:
        raise ValueError(f"User identity not set for peer {peer_id}. Must create or link account first.")

    peer_shared_id = identity['peer_shared_id']
    user_id = identity['user_id']

    # Check if user has been removed from the network
    if user.is_removed(user_id, peer_id, db):
        raise ValueError(f"User {user_id} has been removed from the network and cannot send messages.")

    # Check that user has a username set before allowing message creation
    # This ensures the author's name can be displayed with their message
    if not user.has_username(user_id, peer_id, db):
        raise ValueError(f"No username found for user {user_id}. Username must be set before sending messages.")

    # Build standardized event structure
    # Note: group_id is NOT included - it's derived from channel_id at projection time
    # This saves ~36 bytes in the event, allowing more space for content
    event_data = {
        'type': 'message',
        'channel_id': channel_id,
        'signed_by': peer_shared_id,  # Device that signed the event (for signature verification)
        'signer_type': 'peer_shared',
        'author_id': user_id,  # User who authored the message content (for display)
        'content': content,
        'created_at': t_ms,
        'disappearing_time_ms': disappearing_time_ms  # TTL setting at creation time (0 = permanent)
    }

    event_id = store.publish(event_data, group_id, peer_id, t_ms, db)

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


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project a single message event into the database.

    Message TTL is calculated from the event's disappearing_time_ms field:
    - If disappearing_time_ms is 0: message is permanent (ttl_ms = 0)
    - If disappearing_time_ms > 0: message expires at created_at + disappearing_time_ms

    The disappearing_time_ms is captured at message creation time, so TTL is
    deterministic and doesn't change if channel settings are updated later.
    """
    log.debug(f"message.project() projecting message_id={event_id}, seen_by={recorded_by}")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get and unwrap event
    event_blob = store.get(event_id, unsafedb)
    if not event_blob:
        log.warning(f"message.project() blob not found for message_id={event_id}")
        return None

    unwrapped, _ = crypto.unwrap(event_blob, recorded_by, db)
    if not unwrapped:
        log.warning(f"message.project() unwrap failed for message_id={event_id}")
        return None  # Already blocked by recorded.project() if keys missing

    event_data = crypto.parse_json(unwrapped)
    log.info(f"message.project() projected message content='{event_data.get('content', '')[:50]}...', id={event_id}")

    # Verify signature - get public key from signed_by peer_shared
    from events.identity import peer_shared
    signed_by = event_data.get('signed_by')
    public_key = peer_shared.get_public_key(signed_by, recorded_by, db)
    if not crypto.verify_event(event_data, public_key):
        return None  # Reject unsigned or invalid signature

    # Extract fields from event
    message_id = event_id
    channel_id = event_data.get('channel_id')
    author_id = event_data.get('author_id')  # user_id (person who authored the content)
    content = event_data.get('content', '')
    created_at = event_data.get('created_at')

    # Derive group_id from channel_id (not stored in event to save space)
    # Channel must exist (checked by recorded.check_deps via channel_id dependency)
    channel_row = safedb.query_one(
        "SELECT group_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (channel_id, recorded_by)
    )
    if not channel_row:
        log.warning(f"message.project() channel {channel_id[:20]}... not found - blocking")
        return None
    group_id = channel_row['group_id']

    # Note: Author dependency (author_id -> user_id) is checked by recorded.check_deps()
    # before projection begins, so we don't need to check here.

    # Calculate TTL from event's disappearing_time_ms (captured at creation time)
    # This is deterministic - same event always produces same TTL
    disappearing_time_ms = event_data.get('disappearing_time_ms', 0) or 0
    if disappearing_time_ms > 0:
        ttl_ms = created_at + disappearing_time_ms
    else:
        # Permanent message (disappearing_time_ms = 0 or not set)
        ttl_ms = 0

    # Extract key_id from blob for efficient purge lookups
    # Key ID is the first 16 bytes of the blob (the hint)
    key_id_bytes = event_blob[:crypto.ID_SIZE]
    key_id_b64 = crypto.b64encode(key_id_bytes)

    # Check if deletion exists (may have arrived before message)
    deletion_check = safedb.query_one(
        "SELECT deleted_by FROM message_deletions WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (message_id, recorded_by)
    )

    if deletion_check:
        # Deletion exists - validate it now that we have the message
        from events.content import message_deletion
        deleted_by = deletion_check['deleted_by']

        if message_deletion.validate(message_id, deleted_by, recorded_by, db):
            log.info(f"message.project() message {message_id[:20]}... has valid deletion - skipping projection")
            # Add to deleted_events so upstream knows it was deleted
            safedb.execute(
                "INSERT OR IGNORE INTO deleted_events (event_id, recorded_by) VALUES (?, ?)",
                (message_id, recorded_by)
            )
            return None  # Don't project the message
        else:
            # Deletion was invalid - remove it
            log.warning(f"message.project() removing invalid deletion for message {message_id[:20]}...")
            safedb.execute(
                "DELETE FROM message_deletions WHERE message_id = ? AND recorded_by = ?",
                (message_id, recorded_by)
            )
            # Continue to project the message normally

    # Insert into messages table with peer and timestamp from recorded
    safedb.execute(
        """INSERT OR IGNORE INTO messages
           (message_id, channel_id, group_id, author_id, signed_by, content, created_at, ttl_ms, key_id, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (message_id, channel_id, group_id, author_id, signed_by, content, created_at, ttl_ms, key_id_b64, recorded_by, recorded_at)
    )

    # Record dependency: message depends on channel (for cascading deletion)
    safedb.execute(
        """INSERT OR IGNORE INTO event_dependencies
           (child_event_id, parent_event_id, recorded_by, dependency_type)
           VALUES (?, ?, ?, ?)""",
        (message_id, channel_id, recorded_by, 'channel')
    )

    return event_id
