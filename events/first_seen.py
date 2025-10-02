"""First-seen event management functions."""
from typing import Any
import json

from events import group, message
import store
import crypto


def is_foreign_local_dep(field: str, event_data: dict[str, Any], seen_by_peer_id: str) -> bool:
    """Check if dependency references another peer's local-only data.

    'Local' is relative to the creator - some deps reference the creator's
    local state (never shared). These should only be checked when we ARE
    the creator, and skipped when we're not.

    Examples:
    - peer_shared.peer_id → references creator's local peer
    - key.peer_id → references owner's local peer

    Args:
        field: Dependency field name (e.g., 'peer_id', 'group_id')
        event_data: The event being processed
        seen_by_peer_id: Who is processing this event

    Returns:
        True if this is a foreign local dep (should skip check), False otherwise
    """
    event_type = event_data.get('type')
    created_by = event_data.get('created_by')

    # Schema: peer_shared and key events have peer_id referencing creator's local peer
    CREATOR_LOCAL_PEER_TYPES = {'peer_shared', 'key'}

    if event_type in CREATOR_LOCAL_PEER_TYPES and field == 'peer_id':
        # Skip only if we're not the creator (foreign local)
        return seen_by_peer_id != created_by

    return False


def check_deps(event_data: dict[str, Any], seen_by_peer_id: str, db: Any) -> list[str]:
    """Check dependencies exist in valid_events for this peer.

    Returns list of missing dependency IDs (empty if all satisfied).
    """
    import logging
    log = logging.getLogger(__name__)

    # Common dependency fields across event types
    # Note: 'key_id' is intentionally excluded because symmetric keys are local-only
    # and not shared between peers. Events encrypted with a key are sent as plaintext
    # during sync responses, so the seeing peer should not be required to possess
    # the creator's key event.
    dep_fields = ['group_id', 'channel_id', 'created_by', 'peer_id']

    missing_deps = []

    for field in dep_fields:
        dep_id = event_data.get(field)
        if not dep_id:
            continue

        # Skip foreign local deps (creator's local state we'll never have)
        if is_foreign_local_dep(field, event_data, seen_by_peer_id):
            log.debug(f"first_seen.check_deps() skipping foreign local dep: {field}={dep_id}")
            continue

        # Check if this dep is valid for this peer
        valid = db.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ? LIMIT 1",
            (dep_id, seen_by_peer_id)
        )
        if not valid:
            log.debug(f"first_seen.check_deps() missing dep: {field}={dep_id} for peer={seen_by_peer_id}")
            missing_deps.append(dep_id)

    if missing_deps:
        log.debug(f"first_seen.check_deps() total missing deps: {missing_deps}")
    else:
        log.debug(f"first_seen.check_deps() all deps satisfied")

    return missing_deps

def project_ids(first_seen_ids: list[str], db: Any) -> list[list[str | None]]:
    """Since `first_seen` is the event that triggers projection, this is the central function for projection."""
    """It calls the necessary project functions in other modules for the given event types."""
    import logging
    log = logging.getLogger(__name__)

    log.info(f"first_seen.project_ids() projecting {len(first_seen_ids)} first_seen events")
    projected_ids = [project(id, db) for id in first_seen_ids]
    log.info(f"first_seen.project_ids() completed projection of {len(first_seen_ids)} events")
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
    first_seen_event = crypto.parse_json(first_seen_blob)
    ref_id = first_seen_event['ref_id']
    seen_by_peer_id = first_seen_event['seen_by']

    import logging
    log = logging.getLogger(__name__)
    log.info(f"first_seen.project(): ref_id={ref_id}, seen_by={seen_by_peer_id}")

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
        queues.blocked.add(first_seen_id, seen_by_peer_id, missing_key_ids, db)
        return [None, first_seen_id]

    # If unwrap returned None but no missing keys, try plaintext parsing
    if plaintext is None:
        try:
            plaintext = event_blob
            event_data = crypto.parse_json(plaintext)
        except:
            # Can't parse - skip projection
            return [None, first_seen_id]
    else:
        event_data = crypto.parse_json(plaintext)

    event_type = event_data.get('type')
    log.info(f"Parsed event data, type={event_type}")

    # Phase 2: Check semantic dependencies
    # Special cases that skip ALL dependency checking:
    # 1. Sync events - they introduce new peers, have no semantic deps
    # 2. Self-created user events with invite proof - creator doesn't have invite as valid event
    #
    # Note: peer_shared and key events now use foreign local dep checking instead of blanket skip
    event_type = event_data.get('type')
    skip_dep_check = False

    if event_type == 'sync':
        skip_dep_check = True  # Sync events introduce new peers, no deps to validate
    elif event_type == 'user' and 'invite_pubkey' in event_data:
        # Self-created user with invite proof: creator doesn't have invite in valid_events
        # (invite comes from out-of-band link, not network sync)
        created_by = event_data.get('created_by')
        if seen_by_peer_id == created_by:
            skip_dep_check = True

    if not skip_dep_check:
        missing_deps = check_deps(event_data, seen_by_peer_id, db)
        if missing_deps:
            # Event dependencies missing - block this event for this peer
            log.info(f"Blocking {event_type} event {ref_id} due to missing deps: {missing_deps}")
            queues.blocked.add(first_seen_id, seen_by_peer_id, missing_deps, db)
            return [None, first_seen_id]

    # All dependencies satisfied - proceed with projection
    projected_id = None
    log.info(f"Projecting event type: {event_type}")

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
    elif event_type == 'sync':
        from events import sync
        sync.project(ref_id, seen_by_peer_id, received_at, db)
        projected_id = ref_id
    elif event_type == 'invite':
        from events import invite
        projected_id = invite.project(ref_id, seen_by_peer_id, received_at, db)
    elif event_type == 'user':
        from events import user
        projected_id = user.project(ref_id, seen_by_peer_id, received_at, db)
    elif event_type == 'prekey':
        from events import prekey
        prekey.project(ref_id, seen_by_peer_id, db)
        projected_id = ref_id
    elif event_type == 'prekey_shared':
        from events import prekey_shared
        projected_id = prekey_shared.project(ref_id, seen_by_peer_id, received_at, db)

    # Mark event as valid for this peer
    log.info(f"Marking {event_type} event {ref_id} as valid for peer {seen_by_peer_id}")
    db.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, seen_by_peer_id) VALUES (?, ?)",
        (ref_id, seen_by_peer_id)
    )

    # Notify blocked queue - unblock events that were waiting for this event
    unblocked_ids = queues.blocked.notify_event_valid(ref_id, seen_by_peer_id, db)
    if unblocked_ids:
        log.info(f"Unblocked {len(unblocked_ids)} events after {ref_id} became valid")
        # Re-project unblocked events recursively
        project_ids(unblocked_ids, db)

    return [projected_id, first_seen_id]


def create(ref_id: str, seen_by_peer_id: str, t_ms: int, db: Any, return_dupes: bool) -> str:
    """Create a first_seen event for the given ref_id and return the first_seen_id."""
    import logging
    log = logging.getLogger(__name__)

    log.debug(f"first_seen.create() creating first_seen: ref_id={ref_id}, seen_by={seen_by_peer_id}, t_ms={t_ms}")

    # Build first_seen event (no created_by, no created_at - deterministic per peer+event)
    event_data = {
        'type': 'first_seen',
        'ref_id': ref_id,
        'seen_by': seen_by_peer_id
    }

    blob = json.dumps(event_data).encode()

    # Store the first_seen blob
    first_seen_id = store.blob(blob, t_ms, return_dupes, db)

    log.debug(f"first_seen.create() stored first_seen_id={first_seen_id}")

    # Projection happens later via explicit project() call
    return first_seen_id
