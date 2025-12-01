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

    Uses pure functional create pattern:
    1. resolve_create_deps() gathers dependencies from DB
    2. create_pure() builds blob (no DB access)
    3. store_create_result() stores and projects

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
    from projectors import resolve_create_deps, store_create_result
    from projectors import message as msg_projector

    log.info(f"message.create() creating message in channel_id={channel_id}, content='{content[:50]}...'")

    # 1. Resolve dependencies (DB queries)
    deps = resolve_create_deps("message", {"channel_id": channel_id}, peer_id, db)

    # Validate user identity is set
    if not deps.get('user_id'):
        raise ValueError(f"User identity not set for peer {peer_id}. Must create or link account first.")

    # 2. Pure create (no DB access)
    result = msg_projector.create_pure(deps, content, t_ms)

    # 3. Store and project
    event_id = store_create_result(result, peer_id, t_ms, db)

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

    Uses pure functional projector from projectors.message.
    """
    log.debug(f"message.project() projecting message_id={event_id}, seen_by={recorded_by}")

    from projectors import resolve, apply_result
    from projectors import message as msg_projector

    safedb = create_safe_db(db, recorded_by=recorded_by)

    input_dict = resolve("message", event_id, recorded_by, recorded_at, db)
    if not input_dict:
        return None

    # Side effect: cleanup invalid deletion
    deletion = input_dict["dependencies"].get("deletion")
    if deletion and not deletion.get("is_valid"):
        safedb.execute(
            "DELETE FROM message_deletions WHERE message_id = ? AND recorded_by = ?",
            (event_id, recorded_by)
        )

    result = msg_projector.project(input_dict)

    if result.blocked or not result.valid:
        return None

    apply_result(result, recorded_by, recorded_at, db)

    if "messages" in result.tables:
        log.info(f"message.project() projected id={event_id[:20]}...")
        return event_id
    return None
