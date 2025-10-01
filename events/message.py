"""Message event type."""
from typing import Any
import crypto
import store
from events import group


def create_message(params: dict[str, Any], db: Any, t_ms: int) -> dict[str, Any]:
    """Create a message event, add it to the store, project it, and return the id and a list of recent messages."""
    import json
    import first_seen

    creator = params['creator']

    # Build standardized event structure
    event_data = {
        'type': 'message',
        'channel_id': params['channel'],
        'group_id': params['group']['id'],
        'created_by': creator,
        'content': params.get('content', ''),
        'created_at': t_ms
    }

    plaintext = json.dumps(event_data).encode()
    key = group.pick_key(params['group']['id'], db)
    blob = crypto.wrap(plaintext, key, db)
    first_seen_id = store.store_with_first_seen(blob, creator, t_ms, db)
    projected_first_seen = first_seen.project(first_seen_id, db)
    channel = params['channel']
    latest = list_messages(channel, db)
    db.commit()
    return {
        'id': first_seen_id,
        'latest': latest
    }


def list_messages(channel_id: int, db: Any, seen_by_peer_id: str) -> list[dict[str, Any]]:
    """List messages in a channel for a specific peer."""
    messages = db.query(
        "SELECT * FROM messages WHERE channel_id = ? AND seen_by_peer_id = ? ORDER BY created_at DESC LIMIT 50",
        (channel_id, seen_by_peer_id)
    )
    return messages


def project(event_id: str, db: Any, seen_by_peer_id: str, received_at: int) -> str | None:
    """Project a single message event into the database."""
    import json

    # Get and unwrap event
    event_blob = store.get(event_id, db)
    if not event_blob:
        return None

    unwrapped = crypto.unwrap(event_blob, db)
    event_data = json.loads(unwrapped)

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

    return event_id