"""First-seen event management functions."""
from typing import Any
import json

from events import group, message
import store
import crypto


def check_deps(event_data: dict[str, Any], seen_by_peer_id: str, db: Any) -> list[str]:
    """Check dependencies exist in valid_events for this peer.

    Returns list of missing dependency IDs (empty if all satisfied).
    """
    # Common dependency fields across event types
    dep_fields = ['group_id', 'channel_id', 'created_by', 'key_id', 'peer_id']

    missing_deps = []

    for field in dep_fields:
        dep_id = event_data.get(field)
        if dep_id:
            # Check if this dep is valid for this peer
            valid = db.query_one(
                "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ? LIMIT 1",
                (dep_id, seen_by_peer_id)
            )
            if not valid:
                missing_deps.append(dep_id)

    return missing_deps

def project_ids(first_seen_ids: list[str], db: Any) -> list[list[str | None]]:
    """Since `first_seen` is the event that triggers projection, this is the central function for projection."""
    """It calls the necessary project functions in other modules for the given event types."""
    projected_ids = [project(id, db) for id in first_seen_ids]
    return projected_ids


def project(first_seen_id: str, db: Any) -> list[str | None]:
    """Project first_seen event with two-phase dependency checking.

    Phase 1: Check encryption keys (block if missing).
    Phase 2: Check event dependencies (block if missing).
    Dispatches to type-specific projector if all deps satisfied.
    """
    from events import peer, channel
    import queues

    # Get first_seen blob from store
    first_seen_blob = store.get(first_seen_id, db)
    if not first_seen_blob:
        return [None, None]

    # Parse first_seen event (plaintext JSON, no unwrap needed)
    first_seen_event = json.loads(first_seen_blob.decode())
    ref_id = first_seen_event['ref_id']
    seen_by_peer_id = first_seen_event['seen_by']

    # Get stored_at from store table as received_at
    store_row = db.query_one("SELECT stored_at FROM store WHERE id = ?", (crypto.b64decode(first_seen_id),))
    received_at = store_row['stored_at'] if store_row else 0

    # Get referenced event blob
    event_blob = store.get(ref_id, db)
    if not event_blob:
        return [None, first_seen_id]

    # Phase 1: Try to unwrap (for encrypted events)
    plaintext, missing_key_ids = crypto.unwrap(event_blob, db)
    if missing_key_ids:
        # Crypto keys missing - block this event for this peer
        queues.blocked.add(ref_id, seen_by_peer_id, missing_key_ids, db)
        return [None, first_seen_id]

    # If unwrap returned None but no missing keys, try plaintext parsing
    if plaintext is None:
        try:
            plaintext = event_blob
            event_data = json.loads(plaintext.decode())
        except:
            # Can't parse - skip projection
            return [None, first_seen_id]
    else:
        event_data = json.loads(plaintext.decode() if isinstance(plaintext, bytes) else plaintext)

    # Phase 2: Check semantic dependencies
    missing_deps = check_deps(event_data, seen_by_peer_id, db)
    if missing_deps:
        # Event dependencies missing - block this event for this peer
        queues.blocked.add(ref_id, seen_by_peer_id, missing_deps, db)
        return [None, first_seen_id]

    # All dependencies satisfied - proceed with projection
    event_type = event_data.get('type')
    projected_id = None

    if event_type == 'message':
        projected_id = message.project(ref_id, seen_by_peer_id, received_at, db)
    elif event_type == 'group':
        projected_id = group.project(ref_id, seen_by_peer_id, received_at, db)
    elif event_type == 'peer':
        peer.project(ref_id, seen_by_peer_id, db)
        projected_id = ref_id
    elif event_type == 'key':
        from events import key
        key.project(ref_id, seen_by_peer_id, db)
        projected_id = ref_id
    elif event_type == 'peer_shared':
        from events import peer_shared
        projected_id = peer_shared.project(ref_id, seen_by_peer_id, received_at, db)
    elif event_type == 'channel':
        channel.project(ref_id, seen_by_peer_id, received_at, db)
        projected_id = ref_id

    # Mark event as valid for this peer
    db.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, seen_by_peer_id) VALUES (?, ?)",
        (ref_id, seen_by_peer_id)
    )

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
    first_seen_id = store.blob(blob, t_ms, return_dupes, db)

    # Projection happens later via explicit project() call
    return first_seen_id