"""Message event type."""
from typing import Any
import crypto
import store
from events import group, peer


def create_message(params: dict[str, Any], t_ms: int, db: Any) -> dict[str, Any]:
    """Create a message event, add it to the store, project it, and return the id and a list of recent messages."""
    import json

    # Extract params
    channel_id = params['channel_id']
    group_id = params['group_id']
    peer_id = params['peer_id']  # Local peer ID
    peer_shared_id = params['peer_shared_id']  # Shareable identity ID
    content = params.get('content', '')
    key_id = params['key_id']

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
    private_key = peer.get_private_key(peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Get key_data for encryption
    key_data = group.pick_key(group_id, db)

    # Wrap (canonicalize + encrypt)
    blob = crypto.wrap(signed_event, key_data, db)

    # Store event with first_seen wrapper and projection
    event_id = store.event(blob, peer_id, t_ms, db)

    # Get latest messages
    latest = list_messages(channel_id, peer_id, db)

    db.commit()

    return {
        'id': event_id,
        'latest': latest
    }


def list_messages(channel_id: int, seen_by_peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List messages in a channel for a specific peer."""
    messages = db.query(
        "SELECT * FROM messages WHERE channel_id = ? AND seen_by_peer_id = ? ORDER BY created_at DESC LIMIT 50",
        (channel_id, seen_by_peer_id)
    )
    return messages


def project(event_id: str, seen_by_peer_id: str, received_at: int, db: Any) -> str | None:
    """Project a single message event into the database."""
    import json

    # Get and unwrap event
    event_blob = store.get(event_id, db)
    if not event_blob:
        return None

    unwrapped, _ = crypto.unwrap(event_blob, db)
    if not unwrapped:
        return None  # Already blocked by first_seen.project() if keys missing

    event_data = crypto.parse_json(unwrapped)

    # Verify signature - get public key from created_by peer_shared
    from events import peer_shared
    created_by = event_data.get('created_by')
    public_key = peer_shared.get_public_key(created_by, seen_by_peer_id, db)
    if not crypto.verify_event(event_data, public_key):
        return None  # Reject unsigned or invalid signature

    # Extract fields from event
    message_id = event_id
    channel_id = event_data.get('channel_id')
    group_id = event_data.get('group_id')
    author_id = event_data.get('created_by')
    content = event_data.get('content', '')
    created_at = event_data.get('created_at')

    # Insert into messages table with peer and timestamp from first_seen
    db.execute(
        """INSERT OR IGNORE INTO messages
           (message_id, channel_id, group_id, author_id, content, created_at, seen_by_peer_id, received_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (message_id, channel_id, group_id, author_id, content, created_at, seen_by_peer_id, received_at)
    )

    # Insert into shareable_events
    db.execute(
        """INSERT OR IGNORE INTO shareable_events (event_id, peer_id, created_at)
           VALUES (?, ?, ?)""",
        (
            event_id,
            author_id,
            created_at
        )
    )

    return event_id