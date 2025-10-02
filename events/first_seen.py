"""First-seen event management functions."""
from typing import Any
import json

from events import group, message
import store
import crypto

def project_ids(first_seen_ids: list[str], db: Any) -> list[list[str | None]]:
    """Since `first_seen` is the event that triggers projection, this is the central function for projection."""
    """It calls the necessary project functions in other modules for the given event types."""
    projected_ids = [project(id, db) for id in first_seen_ids]
    return projected_ids


def project(first_seen_id: str, db: Any) -> list[str | None]:
    """Project a first_seen event by dispatching to the referenced event's projector."""
    from events import peer, channel

    # Get first_seen blob from store
    first_seen_blob = store.get(first_seen_id, db)
    if not first_seen_blob:
        return [None, None]

    # Parse first_seen event (plaintext JSON, no unwrap needed)
    first_seen_event = json.loads(first_seen_blob.decode())
    ref_id = first_seen_event['ref_id']
    seen_by_peer_id = first_seen_event['seen_by']

    # Get stored_at from store table as received_at
    import base64
    store_row = db.query_one("SELECT stored_at FROM store WHERE id = ?", (base64.b64decode(first_seen_id),))
    received_at = store_row['stored_at'] if store_row else 0

    # Get referenced event blob
    event_blob = store.get(ref_id, db)
    if not event_blob:
        return [None, first_seen_id]

    # Try to unwrap (for encrypted events) or parse as plaintext
    try:
        unwrapped = crypto.unwrap(event_blob, db)
        if unwrapped:
            event_data = json.loads(unwrapped.decode() if isinstance(unwrapped, bytes) else unwrapped)
        else:
            # If unwrap failed, try plaintext
            event_data = json.loads(event_blob.decode())
    except:
        # Fallback to plaintext parsing
        event_data = json.loads(event_blob.decode())

    # Dispatch to type-specific projector
    event_type = event_data.get('type')
    projected_id = None

    if event_type == 'message':
        projected_id = message.project(ref_id, seen_by_peer_id, received_at, db)
    elif event_type == 'group':
        projected_id = group.project(ref_id, seen_by_peer_id, received_at, db)
    elif event_type == 'peer':
        peer.project(ref_id, seen_by_peer_id, received_at, db)
        projected_id = ref_id
    elif event_type == 'channel':
        channel.project(ref_id, seen_by_peer_id, received_at, db)
        projected_id = ref_id

    return [projected_id, first_seen_id]


def create(ref_id: str, seen_by_peer_id: str, t_ms: int, db: Any, return_dupes: bool) -> str:
    """Create a first_seen event for the given ref_id and return the first_seen_id."""
    # Build first_seen event (no created_by, no created_at - deterministic per peer+event)
    event_data = {
        'type': 'first_seen',
        'ref_id': ref_id,
        'seen_by': seen_by_peer_id
    }

    blob = json.dumps(event_data).encode()

    # Store the first_seen blob
    first_seen_id = store.store(blob, t_ms, return_dupes, db)

    # Projection happens later via explicit project() call
    return first_seen_id