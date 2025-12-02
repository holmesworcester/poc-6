"""Message event type."""

# Registry metadata
EVENT_TYPE = 'message'
SHAREABLE = True  # Messages sync to channel members
EPHEMERAL = False
PROJECTION_TABLE = ('messages', 'message_id')

from typing import Any
import logging
import crypto
import store
from events.group import group
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)

# Default message TTL: 1 week (in milliseconds)
DEFAULT_MESSAGE_TTL_MS = 7 * 24 * 60 * 60 * 1000


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
    channel_row = safedb.query_one(
        "SELECT group_id, disappearing_time_ms FROM channels WHERE channel_id = ? AND recorded_by = ? LIMIT 1",
        (channel_id, peer_id)
    )
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

    # Build standardized event structure
    event_data = {
        'type': 'message',
        'channel_id': channel_id,
        'group_id': group_id,
        'signed_by': peer_shared_id,  # Device that signed the event (for signature verification)
        'author_id': user_id,  # User who authored the message content (for display)
        'content': content,
        'created_at': t_ms,
        'disappearing_time_ms': disappearing_time_ms  # TTL setting at creation time (0 = permanent)
    }

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Get key_data for encryption (group.pick_key uses peer_id for access control)
    key_data = group.pick_key(group_id, peer_id, db)

    # Wrap (canonicalize + encrypt)
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, key_data, db)

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


def list(channel_id: int, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List messages in a channel for a specific peer, including attachments, author names, and reactions.

    Returns message dicts with 'attachments' field containing list of attached files,
    'author_name' field from the users table, and 'reactions' field from message_reactions:
    [{'message_id', 'content', 'author_id', 'author_name', 'created_at', 'attachments': [...], 'reactions': [...]}, ...]
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    messages = safedb.query(
        """SELECT m.*, u.name as author_name
           FROM messages m
           LEFT JOIN users u ON m.author_id = u.user_id AND m.recorded_by = u.recorded_by
           WHERE m.channel_id = ? AND m.recorded_by = ?
           ORDER BY m.created_at DESC LIMIT 50""",
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
    group_id = event_data.get('group_id')
    author_id = event_data.get('author_id')  # user_id (person who authored the content)
    content = event_data.get('content', '')
    created_at = event_data.get('created_at')

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
