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
    seen_id = store.with_seen(blob, creator, t_ms, db)
    projected_seen = store.project(seen_id, db)
    channel = params['channel']
    latest = list_messages(channel, db)
    db.commit()
    return {
        'id': seen_id,
        'latest': latest
    }


def list_messages(channel_id: int, db: Any) -> list[dict[str, Any]]:
    """List messages in a channel."""
    messages = db.query("SELECT * FROM messages WHERE channel_id = ? ORDER BY created_at DESC LIMIT 50", (channel_id,))
    return messages