"""Message event type."""
from typing import Any
import crypto
import store
import group


def create_message(params: dict[str, Any], db: Any, t_ms: int) -> dict[str, Any]:
    """Create a message event, add it to the store, project it, and return the id and a list of recent messages."""
    creator = params['creator']
    props = params
    plaintext = props
    key = group.pick_key(params['group']['id'], db)
    blob = crypto.wrap(plaintext, key, db)
    import first_seen
    first_seen_id = store.store_with_first_seen(blob, creator, t_ms, db)
    projected_first_seen = first_seen.project(first_seen_id, db)
    channel = params['channel']
    latest = list_messages(channel, db)
    db.commit()
    return {
        'id': first_seen_id,
        'latest': latest
    }


def list_messages(channel_id: int, db: Any) -> list[dict[str, Any]]:
    """List messages in a channel."""
    messages = db.query("SELECT * FROM messages WHERE channel_id = ? ORDER BY created_at DESC LIMIT 50", (channel_id,))
    return messages


def project(event_id: str, db: Any) -> str | None:
    """Project a single message event into the database."""
    import json

    # Get the event blob from store
    event_blob = store.get(event_id, db)
    if not event_blob:
        return None

    # Unwrap and parse the event
    unwrapped = crypto.unwrap(event_blob, db)
    event_data = json.loads(unwrapped)

    # Extract message fields
    message_id = event_id
    channel_id = event_data.get('channel_id')
    group_id = event_data.get('group_id', '')
    author_id = event_data.get('created_by', event_data.get('creator'))
    content = event_data.get('content', '')
    created_at = event_data.get('created_at', event_data.get('t_ms'))

    # Insert into messages table
    db.execute(
        """INSERT OR IGNORE INTO messages
           (message_id, channel_id, group_id, author_id, content, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (message_id, channel_id, group_id, author_id, content, created_at)
    )

    return event_id