"""Message event type."""
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
    """List messages in a channel for a specific peer, including attachments and author names.

    Returns message dicts with 'attachments' field containing list of attached files
    and 'author_name' field from the users table:
    [{'message_id', 'content', 'author_id', 'author_name', 'created_at', 'attachments': [...]}, ...]
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

    # Enrich each message with attachments
    for msg in messages:
        attachments = safedb.query(
            """SELECT ma.file_id, ma.filename, ma.mime_type, ma.blob_bytes
               FROM message_attachments ma
               WHERE ma.message_id = ? AND ma.recorded_by = ?
               ORDER BY ma.recorded_at ASC""",
            (msg['message_id'], recorded_by)
        )
        msg['attachments'] = attachments if attachments else []

    return messages


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project a single message event into the database.

    Uses pure functional projector - all logic is in pure_projectors.message.
    This wrapper handles:
    1. Building the input dict via resolver
    2. Applying the pure projector's output to the database
    3. Handling side effects (invalid deletion cleanup)
    """
    log.debug(f"message.project() projecting message_id={event_id}, seen_by={recorded_by}")

    from pure_projectors import resolver
    from pure_projectors.message import project as pure_project
    from pure_projectors.framework import apply_result

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Build input dict for pure projector
    input_dict = resolver.resolve_message(event_id, recorded_by, recorded_at, db)
    if not input_dict:
        log.warning(f"message.project() resolver failed for message_id={event_id}")
        return None

    # Handle invalid deletion side-effect (cleanup)
    # This is a side effect that can't be expressed in the pure output
    deletion = input_dict["dependencies"].get("deletion")
    if deletion and not deletion.get("is_valid"):
        log.warning(f"message.project() removing invalid deletion for message {event_id[:20]}...")
        safedb.execute(
            "DELETE FROM message_deletions WHERE message_id = ? AND recorded_by = ?",
            (event_id, recorded_by)
        )

    # Run pure projector
    result = pure_project(input_dict)

    if result.blocked:
        log.info(f"message.project() blocked, missing deps: {result.missing_deps}")
        return None

    if not result.valid:
        log.warning(f"message.project() validation failed: {result.reason}")
        return None

    # Apply result to database
    apply_result(result, recorded_by, recorded_at, db)

    # Return event_id if we projected, None if deleted
    if "messages" in result.tables:
        log.info(f"message.project() projected message content='{input_dict['event_data'].get('content', '')[:50]}...', id={event_id}")
        return event_id
    else:
        log.info(f"message.project() message {event_id[:20]}... has valid deletion - skipping projection")
        return None
