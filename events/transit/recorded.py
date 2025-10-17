"""Recorded event management functions."""
from typing import Any
import json

from events.group import group
from events.content import message
import store
import crypto
from db import create_safe_db, create_unsafe_db


def is_foreign_local_dep(field: str, event_data: dict[str, Any], recorded_by: str) -> bool:
    """Check if dependency references another peer's local-only data.

    'Local' is relative to the creator - some deps reference the creator's
    local state (never shared). These should only be checked when we ARE
    the creator, and skipped when we're not.

    Examples:
    - peer_shared.peer_id → references creator's local peer
    - key.peer_id → references owner's local peer
    - sync.peer_id → references requester's local peer

    Args:
        field: Dependency field name (e.g., 'peer_id', 'group_id')
        event_data: The event being processed
        recorded_by: Who recorded/is processing this event

    Returns:
        True if this is a foreign local dep (should skip check), False otherwise
    """
    event_type = event_data.get('type')
    created_by = event_data.get('created_by')

    # Schema: key events have created_by referencing creator's local peer (not peer_shared_id)
    # peer_shared events have peer_id referencing creator's local peer
    LOCAL_CREATOR_TYPES = {'transit_key', 'group_key', 'transit_prekey', 'group_prekey'}

    if event_type in LOCAL_CREATOR_TYPES and field == 'created_by':
        # Local events have created_by=peer_id (local). Skip if we're not the creator (foreign local)
        return recorded_by != created_by

    if event_type == 'peer_shared' and field == 'peer_id':
        # peer_shared events have peer_id referencing creator's local peer
        # The creator is the peer whose identity is being shared (peer_id field)
        creator_peer_id = event_data.get('peer_id')
        is_foreign = recorded_by != creator_peer_id
        return is_foreign

    # Sync events have peer_id referencing requester's local peer (always foreign)
    if event_type == 'sync' and field == 'peer_id':
        return True

    return False


def check_deps(event_data: dict[str, Any], recorded_by: str, db: Any) -> list[str]:
    """Check dependencies exist in valid_events for this peer.

    Returns list of missing dependency IDs (empty if all satisfied).
    """
    import logging
    log = logging.getLogger(__name__)

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Common dependency fields across event types
    # Note: 'key_id' is intentionally excluded because symmetric keys are local-only
    # and not shared between peers. Events encrypted with a key are sent as plaintext
    # during sync responses, so the recording peer should not be required to possess
    # the creator's key event.
    dep_fields = ['group_id', 'channel_id', 'created_by', 'peer_id', 'peer_shared_id', 'invite_id']

    # User events reference invite_id (which contains group/channel metadata)
    # They create stub group/channel rows from the invite during projection
    event_type = event_data.get('type')
    if event_type == 'user':
        dep_fields = ['created_by', 'peer_id', 'invite_id']  # Depend on invite, not group/channel

    missing_deps = []

    for field in dep_fields:
        dep_id = event_data.get(field)
        if not dep_id:
            continue

        # Skip foreign local deps (creator's local state we'll never have)
        if is_foreign_local_dep(field, event_data, recorded_by):
            log.debug(f"recorded.check_deps() skipping foreign local dep: {field}={dep_id}")
            continue

        # Check if this dep is valid for this peer
        valid = safedb.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ? LIMIT 1",
            (dep_id, recorded_by)
        )
        if not valid:
            log.warning(f"recorded.check_deps() missing dep: {field}={dep_id[:20]}... for peer={recorded_by[:20]}...")
            missing_deps.append(dep_id)

    if missing_deps:
        log.debug(f"recorded.check_deps() total missing deps: {missing_deps}")
    else:
        log.debug(f"recorded.check_deps() all deps satisfied")

    return missing_deps

def project_ids(recorded_ids: list[str], db: Any, _recursion_depth: int = 0) -> list[list[str | None]]:
    """Since `recorded` is the event that triggers projection, this is the central function for projection."""
    """It calls the necessary project functions in other modules for the given event types."""
    import logging
    log = logging.getLogger(__name__)

    if _recursion_depth > 100:
        log.error(f"[PROJECT_IDS] RECURSION LIMIT EXCEEDED depth={_recursion_depth} - possible infinite loop!")
        return []

    log.info(f"recorded.project_ids() projecting {len(recorded_ids)} recorded events (depth={_recursion_depth})")
    projected_ids = [project(id, db, _recursion_depth) for id in recorded_ids]
    log.info(f"recorded.project_ids() completed projection of {len(recorded_ids)} events (depth={_recursion_depth})")
    return projected_ids


def project(recorded_id: str, db: Any, _recursion_depth: int = 0) -> list[str | None]:
    """Project recorded event with two-phase dependency checking.

    Phase 1: Check encryption keys (block if missing).
    Phase 2: Check event dependencies (block if missing).
    Dispatches to type-specific projector if all deps satisfied.
    """
    from events.identity import peer
    from events.content import channel
    import queues

    unsafedb = create_unsafe_db(db)

    # Get recorded blob from store
    recorded_blob = store.get(recorded_id, unsafedb)
    if not recorded_blob:
        log.warning(f"recorded.project(): blob not found for recorded_id={recorded_id[:30]}...")
        return [None, None]

    # Parse recorded event (plaintext JSON, no unwrap needed)
    recorded_event = crypto.parse_json(recorded_blob)
    ref_id = recorded_event['ref_id']
    recorded_by = recorded_event['recorded_by']

    import logging
    log = logging.getLogger(__name__)
    log.info(f"recorded.project(): ref_id={ref_id[:20]}..., recorded_by={recorded_by[:20]}..., recorded_id={recorded_id[:20]}...")

    # DEBUG: Check if this is a sync request that we're about to process
    temp_type = None
    temp_blob = store.get(ref_id, unsafedb)
    if temp_blob:
        try:
            temp_data = crypto.parse_json(temp_blob)
            temp_type = temp_data.get('type')
            if temp_type == 'sync':
                log.info(f"recorded.project(): SYNC EVENT FOUND! Processing sync request recorded_by={recorded_by[:20]}...")
        except:
            pass

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get stored_at from store table as recorded_at
    store_row = unsafedb.query_one("SELECT stored_at FROM store WHERE id = ?", (recorded_id,))
    recorded_at = store_row['stored_at'] if store_row else 0

    # Get referenced event blob
    event_blob = store.get(ref_id, unsafedb)
    if not event_blob:
        return [None, recorded_id]

    # Phase 1: Try to unwrap (for encrypted events)
    plaintext, missing_key_ids = crypto.unwrap_event(event_blob, recorded_by, db)

    # DEBUG: Check if sync events are being blocked on keys
    if temp_type == 'sync':
        log.info(f"recorded.project(): SYNC unwrap result: plaintext={'YES' if plaintext else 'NO'}, missing_keys={missing_key_ids}")

    # Parse event data to determine type (needed for shareable check)
    event_data = None
    event_type = None

    if plaintext:
        # Successfully decrypted or was plaintext
        event_data = crypto.parse_json(plaintext)
        event_type = event_data.get('type')
        if temp_type == 'sync':
            log.info(f"recorded.project(): SYNC parsed, event_type={event_type}")
    elif not missing_key_ids:
        # Not encrypted, try plaintext parsing
        try:
            plaintext = event_blob
            event_data = crypto.parse_json(plaintext)
            event_type = event_data.get('type')
        except Exception as e:
            # Can't parse - skip projection
            print(f"DEBUG PARSE FAILURE: ref_id={ref_id[:20] if ref_id else 'N/A'}..., error={str(e)[:50]}")
            return [None, recorded_id]

    log.info(f"Parsed event data, type={event_type}")

    # Mark non-local-only events as shareable (centralized marking)
    # This happens BEFORE blocking so blocked events (crypto or semantic deps) are still shareable
    # Track that this peer recorded this event and can share it (not who created it)
    LOCAL_ONLY_TYPES = {'peer', 'transit_key', 'group_key', 'transit_prekey', 'group_prekey', 'recorded', 'network_created', 'network_joined', 'invite_accepted'}

    should_mark_shareable = False
    if event_type:
        # We know the type - check if it's shareable
        should_mark_shareable = event_type not in LOCAL_ONLY_TYPES
    elif missing_key_ids:
        # Encrypted blob we can't decrypt - but local events are never encrypted!
        # So this must be shareable
        should_mark_shareable = True

    if should_mark_shareable:
        from events.transit import sync
        sync.add_shareable_event(
            ref_id,
            recorded_by,  # This peer recorded the event and can share it
            event_data.get('created_at', recorded_at) if event_data else recorded_at,
            recorded_at,
            db
        )

    # Handle crypto blocking (after shareable marking)
    # Block events we can't decrypt - they'll still be shareable and sent during sync
    if missing_key_ids:
        queues.blocked.add(recorded_id, recorded_by, missing_key_ids, safedb)
        return [None, recorded_id]

    # If we got here without event_data, return early
    if not event_data:
        return [None, recorded_id]

    # Phase 2: Check semantic dependencies
    # Special case: Self-created user events with invite proof - creator doesn't have invite as valid event
    # (invite comes from out-of-band link, not network sync)
    event_type = event_data.get('type')
    skip_dep_check = False

    if event_type == 'user' and 'invite_pubkey' in event_data:
        # Self-created user with invite proof: creator doesn't have invite in valid_events
        # (invite comes from out-of-band link, not network sync)
        created_by = event_data.get('created_by')
        if recorded_by == created_by:
            skip_dep_check = True

    if event_type == 'invite':
        # invite events received from out-of-band invite links are root-of-trust for joiners
        # When Bob records Alice's invite, he doesn't need Alice's group/channel to be valid
        # (he trusts the invite link itself as the credential)
        created_by = event_data.get('created_by')
        if recorded_by != created_by:  # Received from out-of-band (not self-created)
            skip_dep_check = True

    if event_type == 'invite_accepted':
        # invite_accepted events are root-of-trust for joiners - they capture out-of-band
        # trusted data from the invite link. Only self-created ones are valid.
        # However, we still need to check created_by dependency (peer event must exist first
        # for foreign key constraints in transit_prekeys table)
        created_by = event_data.get('created_by')
        print(f"DEBUG invite_accepted: recorded_by={recorded_by[:20]}..., created_by={created_by[:20] if created_by else 'N/A'}...")
        # Do NOT skip dep check - we need peer event to be projected first

    print(f"DEBUG: Before dep check, skip_dep_check={skip_dep_check}, event_type={event_type}")
    if not skip_dep_check:
        print(f"DEBUG: Checking dependencies for {event_type}")
        missing_deps = check_deps(event_data, recorded_by, db)
        if missing_deps:
            # Event dependencies missing - block this event for this peer
            requester_peer_shared_id = event_data.get('peer_shared_id', 'N/A')
            log.warning(f"Blocking {event_type} event {ref_id[:20]}... recorded_by={recorded_by[:20]}... requester_peer_shared={requester_peer_shared_id[:20]}... missing deps: {[d[:20] for d in missing_deps]}")
            print(f"DEBUG BLOCKING: {event_type} event blocked, ref_id={ref_id[:20]}..., recorded_by={recorded_by[:20]}..., missing deps={[d[:20] + '...' for d in missing_deps]}")
            queues.blocked.add(recorded_id, recorded_by, missing_deps, safedb)
            return [None, recorded_id]
    else:
        print(f"DEBUG: Skipping dep check for {event_type}")

    # All dependencies satisfied - proceed with projection
    projected_id = None
    log.info(f"Projecting event type: {event_type}")
    print(f"DEBUG: About to dispatch event_type={event_type}, ref_id={ref_id[:20] if ref_id else 'N/A'}..., recorded_by={recorded_by[:20]}...")

    if event_type == 'message':
        projected_id = message.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'group':
        projected_id = group.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'peer':
        peer.project(ref_id, recorded_by, db)
        projected_id = ref_id
    elif event_type == 'transit_key':
        from events.transit import transit_key
        transit_key.project(ref_id, recorded_by, db)
        projected_id = ref_id
    elif event_type == 'group_key':
        from events.group import group_key
        group_key.project(ref_id, recorded_by, db)
        projected_id = ref_id
    elif event_type == 'peer_shared':
        from events.identity import peer_shared
        projected_id = peer_shared.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'channel':
        channel.project(ref_id, recorded_by, recorded_at, db)
        projected_id = ref_id
    elif event_type == 'sync':
        from events.transit import sync
        sync.project(ref_id, recorded_by, recorded_at, db)
        projected_id = ref_id
    elif event_type == 'invite':
        from events.identity import invite
        projected_id = invite.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'user':
        from events.identity import user
        projected_id = user.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'transit_prekey':
        from events.transit import transit_prekey
        transit_prekey.project(ref_id, recorded_by, recorded_at, db)
        projected_id = ref_id
    elif event_type == 'transit_prekey_shared':
        from events.transit import transit_prekey_shared
        projected_id = transit_prekey_shared.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'group_prekey':
        from events.group import group_prekey
        group_prekey.project(ref_id, recorded_by, recorded_at, db)
        projected_id = ref_id
    elif event_type == 'group_prekey_shared':
        from events.group import group_prekey_shared
        projected_id = group_prekey_shared.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'group_key_shared':
        from events.group import group_key_shared
        projected_id = group_key_shared.project(ref_id, recorded_by, recorded_at, db)
        if projected_id is None:
            # Projection failed (can't decrypt - event not for us)
            # Don't mark as valid, but it's still shareable
            log.info(f"group_key_shared projection returned None (not for us), skipping valid marking")
            return [None, recorded_id]
    elif event_type == 'invite_accepted':
        from events.identity import invite_accepted
        log.warning(f"DISPATCHER: Calling invite_accepted.project() for ref_id={ref_id[:20]}..., recorded_by={recorded_by[:20]}...")
        invite_accepted.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'group_member':
        from events.group import group_member
        projected_id = group_member.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'network_created':
        from events.identity import network_created
        projected_id = network_created.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'network_joined':
        from events.identity import network_joined
        projected_id = network_joined.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'address':
        from events.identity import address
        projected_id = address.project(ref_id, recorded_by, recorded_at, db)

    # Mark event as valid for this peer
    log.warning(f"[VALID_EVENT] Marking {event_type} event {ref_id[:20]}... as valid for peer {recorded_by[:20]}...")

    # Check if blob is in store before marking as valid
    in_store = db.query_one("SELECT 1 FROM store WHERE id = ?", (ref_id,))
    if not in_store:
        log.error(f"[VALID_EVENT_BUG] ❌ Marking event {ref_id[:20]}... as valid but blob NOT in store! type={event_type}")

    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (ref_id, recorded_by)
    )

    # Notify blocked queue - unblock events that were waiting for this event
    unblocked_ids = queues.blocked.notify_event_valid(ref_id, recorded_by, safedb)
    if unblocked_ids:
        log.warning(f"Unblocked {len(unblocked_ids)} events after {ref_id[:20]}... became valid for peer {recorded_by[:20]}...")
        # Re-project unblocked events recursively
        project_ids(unblocked_ids, db, _recursion_depth + 1)
    else:
        log.debug(f"No events to unblock after {ref_id[:20]}... for peer {recorded_by[:20]}...")

    return [projected_id, recorded_id]


def create(ref_id: str, recorded_by: str, t_ms: int, db: Any, return_dupes: bool) -> str:
    """Create a recorded event for the given ref_id and return the recorded_id."""
    import logging
    log = logging.getLogger(__name__)

    log.debug(f"recorded.create() creating recorded event: ref_id={ref_id}, recorded_by={recorded_by}, t_ms={t_ms}")

    # Log ALL recorded event creations with ref_id and recorded_by for debugging
    log.info(f">>> recorded.create(): ref_id={ref_id[:20]}..., recorded_by={recorded_by[:20]}...")

    # Build recorded event (no created_by, no created_at - deterministic per peer+event)
    event_data = {
        'type': 'recorded',
        'ref_id': ref_id,
        'recorded_by': recorded_by
    }

    blob = json.dumps(event_data).encode()

    unsafedb = create_unsafe_db(db)

    # Store the recorded blob
    recorded_id = store.blob(blob, t_ms, return_dupes, unsafedb)

    log.debug(f"recorded.create() stored recorded_id={recorded_id}")

    # Projection happens later via explicit project() call
    return recorded_id
