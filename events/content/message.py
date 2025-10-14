"""Message event type."""
from typing import Any
import logging
import crypto
import store
from events.group import group
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(peer_id: str, channel_id: str, content: str, t_ms: int, db: Any) -> dict[str, Any]:
    """Create a message event, add it to the store, project it, and return the id and a list of recent messages.

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

    Returns:
        {'id': message_id, 'latest': list of recent messages}
    """
    log.info(f"message.create() creating message in channel_id={channel_id}, content='{content[:50]}...'")

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Query channel to get group_id
    channel_row = safedb.query_one(
        "SELECT group_id FROM channels WHERE channel_id = ? AND recorded_by = ? LIMIT 1",
        (channel_id, peer_id)
    )
    if not channel_row:
        raise ValueError(f"Channel {channel_id} not found for peer {peer_id}")

    group_id = channel_row['group_id']

    # Query peer_self to get peer_shared_id (subjective table)
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_id, peer_id)
    )
    if not peer_self_row or not peer_self_row['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found or peer_shared_id not set in peer_self table")

    peer_shared_id = peer_self_row['peer_shared_id']

    # Build standardized event structure
    event_data = {
        'type': 'message',
        'channel_id': channel_id,
        'group_id': group_id,
        'created_by': peer_shared_id,  # References shareable peer identity
        'content': content,
        'created_at': t_ms
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

    # Get latest messages
    latest = list_messages(channel_id, peer_id, db)

    # Commit through unsafedb (commit is device-wide, not peer-scoped)
    unsafedb = create_unsafe_db(db)
    unsafedb.commit()

    return {
        'id': event_id,
        'latest': latest
    }


def list_messages(channel_id: int, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List messages in a channel for a specific peer."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    messages = safedb.query(
        "SELECT * FROM messages WHERE channel_id = ? AND recorded_by = ? ORDER BY created_at DESC LIMIT 50",
        (channel_id, recorded_by)
    )
    return messages


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project a single message event into the database."""
    import json
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

    # Verify signature - get public key from created_by peer_shared
    from events.identity import peer_shared
    created_by = event_data.get('created_by')
    public_key = peer_shared.get_public_key(created_by, recorded_by, db)
    if not crypto.verify_event(event_data, public_key):
        return None  # Reject unsigned or invalid signature

    # Extract fields from event
    message_id = event_id
    channel_id = event_data.get('channel_id')
    group_id = event_data.get('group_id')
    author_id = event_data.get('created_by')
    content = event_data.get('content', '')
    created_at = event_data.get('created_at')

    # Insert into messages table with peer and timestamp from recorded
    safedb.execute(
        """INSERT OR IGNORE INTO messages
           (message_id, channel_id, group_id, author_id, content, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (message_id, channel_id, group_id, author_id, content, created_at, recorded_by, recorded_at)
    )

    return event_id