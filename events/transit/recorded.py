"""Recorded event management functions."""
from typing import Any
import json
import logging

from events.group import group
from events.content import message
import store
import crypto
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def _get_authoritative_created_at(event_type: str, event_id: str, recorded_by: str, safedb: Any) -> int | None:
    """Get authoritative created_at from projection table.

    Maps each event type to its projection table and queries for the true created_at value
    that was stored during projection. Returns None if event type doesn't project to a table
    or the row wasn't found.

    Args:
        event_type: The event type (e.g., 'channel', 'group', 'message')
        event_id: The event ID
        recorded_by: The peer who recorded this event
        safedb: Safe database connection

    Returns:
        The authoritative created_at timestamp, or None if not found
    """
    # Map event types to (table_name, id_column_name)
    TABLE_MAP = {
        'channel': ('channels', 'channel_id'),
        'group': ('groups', 'group_id'),
        'peer_shared': ('peers_shared', 'peer_shared_id'),
        'user': ('users', 'user_id'),
        'transit_prekey_shared': ('transit_prekeys_shared', 'transit_prekey_shared_id'),
        'group_prekey_shared': ('group_prekeys_shared', 'group_prekey_shared_id'),
        'group_key_shared': ('group_keys_shared', 'group_key_shared_id'),
        'invite': ('invites', 'invite_id'),
        'message': ('messages', 'message_id'),
        'message_deletion': ('message_deletions', 'deletion_id'),
        'address': ('addresses', 'address_id'),
        'group_member': ('group_members', 'user_id'),
        # Note: file_slice and message_attachment are NOT included here because:
        # - file_slice: syncs separately, no created_at in projection table
        # - message_attachment: syncs separately, no created_at in projection table
    }

    if event_type not in TABLE_MAP:
        return None

    table, id_col = TABLE_MAP[event_type]

    try:
        row = safedb.query_one(
            f"SELECT created_at FROM {table} WHERE {id_col} = ? AND recorded_by = ?",
            (event_id, recorded_by)
        )
        return row['created_at'] if row else None
    except Exception as e:
        log.debug(f"Failed to get authoritative created_at for {event_type} {event_id[:20]}...: {e}")
        return None


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

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Common dependency fields across event types
    # Note: 'key_id' is intentionally excluded because symmetric keys are local-only
    # and not shared between peers. Events encrypted with a key are sent as plaintext
    # during sync responses, so the recording peer should not be required to possess
    # the creator's key event.
    dep_fields = ['group_id', 'channel_id', 'created_by', 'peer_id', 'peer_shared_id', 'invite_id', 'message_id', 'user_id']

    # User events reference invite_id (which contains group/channel metadata)
    # They create stub group/channel rows from the invite during projection
    event_type = event_data.get('type')
    if event_type == 'user':
        dep_fields = ['created_by', 'peer_id', 'invite_id']  # Depend on invite, not group/channel
    elif event_type == 'network':
        # Network events depend on both groups and the creator user
        dep_fields = ['created_by', 'all_users_group_id', 'admins_group_id', 'creator_user_id']
    elif event_type == 'invite':
        # Invite events need creator to exist for signature verification
        # Network/group/channel are metadata and don't need to exist yet
        dep_fields = ['created_by']
    elif event_type == 'message_deletion':
        # Deletion events only depend on the creator (for signature verification)
        # Message doesn't need to exist - deletion can arrive before the message
        dep_fields = ['created_by']

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

    if _recursion_depth > 100:
        log.error(f"[PROJECT_IDS] RECURSION LIMIT EXCEEDED depth={_recursion_depth} - possible infinite loop!")
        return []

    log.info(f"recorded.project_ids() projecting {len(recorded_ids)} recorded events (depth={_recursion_depth})")
    projected_ids = []
    for recorded_id in recorded_ids:
        try:
            result = project(recorded_id, db, _recursion_depth)
            projected_ids.append(result)
        except Exception as e:
            log.error(f"[PROJECT_IDS_EXCEPTION] ❌ EXCEPTION projecting recorded_id={recorded_id[:20]}... depth={_recursion_depth}: {str(e)[:200]}")
            import traceback
            traceback.print_exc()
            raise  # Re-raise to fail immediately so we can see the error
    log.info(f"recorded.project_ids() completed projection of {len(recorded_ids)} events (depth={_recursion_depth})")
    return projected_ids


def project(recorded_id: str, db: Any, _recursion_depth: int = 0, _triggered_by: str = 'initial') -> list[str | None]:
    """Project recorded event with two-phase dependency checking.

    Phase 1: Check encryption keys (block if missing).
    Phase 2: Check event dependencies (block if missing).
    Dispatches to type-specific projector if all deps satisfied.

    Args:
        _triggered_by: What triggered this projection (for debugging causality)
    """
    from events.identity import peer
    from events.content import channel
    import queues
    import json
    from tests.utils import timeline

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

    # Timeline: Log projection start
    timeline.log('proj_start', ref_id=ref_id, ref_type=temp_type, recorded_by=recorded_by,
                 triggered_by=_triggered_by, depth=_recursion_depth)

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

    # DEBUG: Log unwrap results for all events to understand what's happening
    log.error(f"[UNWRAP_RESULT] ref_id={ref_id[:20]}... plaintext={'YES' if plaintext else 'NO'}, missing_keys={missing_key_ids}, temp_type={temp_type}")

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
            log.warning(f"Failed to parse event: ref_id={ref_id[:20] if ref_id else 'N/A'}..., error={str(e)[:50]}")
            return [None, recorded_id]

    log.info(f"Parsed event data, type={event_type}")

    # Timeline: Log event type and plaintext data for debugging
    if event_data:
        import json
        # Truncate large fields for readability
        timeline_data = {}
        for k, v in event_data.items():
            if k in ('ciphertext', 'blob', 'data') and isinstance(v, (str, bytes)) and len(str(v)) > 50:
                timeline_data[k] = f"{str(v)[:50]}..."
            else:
                timeline_data[k] = v
        timeline.log('event_data', ref_id=ref_id, ref_type=event_type, recorded_by=recorded_by,
                    data=json.dumps(timeline_data, default=str))

    # DEBUG: Log if this is a channel event
    if event_type == 'channel':
        log.error(f"[CHANNEL_AFTER_PARSE] type=channel, ref_id={ref_id[:20]}..., recorded_by={recorded_by[:20]}...")

    # Mark non-local-only events as shareable (centralized marking)
    # This happens BEFORE blocking so blocked events (crypto or semantic deps) are still shareable
    # Track that this peer recorded this event and can share it (not who created it)
    LOCAL_ONLY_TYPES = {'peer', 'transit_key', 'group_key', 'transit_prekey', 'group_prekey', 'recorded', 'network_created', 'network_joined', 'invite_accepted', 'bootstrap_complete', 'sync_connect'}

    should_mark_shareable = False
    if event_type:
        # We know the type - check if it's shareable
        should_mark_shareable = event_type not in LOCAL_ONLY_TYPES
    elif missing_key_ids:
        # Encrypted blob we can't decrypt - but local events are never encrypted!
        # So this must be shareable
        should_mark_shareable = True

    if should_mark_shareable:
        # Mark event as shareable in shareable_events table
        # Always use created_at=None for simplicity and determinism
        # Sync protocol doesn't need created_at - it uses recorded_at for ordering
        # UI lazy loading will use separate projected_events table with created_at
        from events.transit import sync
        log.debug(f"Adding {event_type or 'unknown'} {ref_id[:20]}... to shareable_events with created_at=None")
        sync.add_shareable_event(
            ref_id,
            recorded_by,
            created_at=None,
            recorded_at=recorded_at,
            db=db
        )

    # Handle crypto blocking (after shareable marking)
    # Block events we can't decrypt - they'll still be shareable and sent during sync
    if missing_key_ids:
        timeline.log('blocked', ref_id=ref_id, ref_type=event_type, recorded_by=recorded_by,
                     status='blocked_crypto', blocked_on=missing_key_ids)
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

    # NOTE: Invite validation moved to invite.project() for better modularity
    # Invites from URLs (with invite_accepted) skip validation in projector
    # Invites from sync are validated (signature, network, creator) in projector

    if event_type == 'invite_accepted':
        # invite_accepted events are root-of-trust for joiners - they capture out-of-band
        # trusted data from the invite link. Only self-created ones are valid.
        # However, we still need to check created_by dependency (peer event must exist first
        # for foreign key constraints in transit_prekeys table)
        created_by = event_data.get('created_by')
        # Do NOT skip dep check - we need peer event to be projected first

    if not skip_dep_check:
        missing_deps = check_deps(event_data, recorded_by, db)
        if missing_deps:
            # Event dependencies missing - block this event for this peer
            requester_peer_shared_id = event_data.get('peer_shared_id', 'N/A')
            log.warning(f"Blocking {event_type} event {ref_id[:20]}... recorded_by={recorded_by[:20]}... requester_peer_shared={requester_peer_shared_id[:20]}... missing deps: {[d[:20] for d in missing_deps]}")

            # DEBUG: If this is a channel event, log the actual dep_ids
            if event_type == 'channel':
                log.error(f"[CHANNEL_BLOCKED] channel_id={ref_id[:20]}... recorded_by={recorded_by[:20]}... missing_deps={missing_deps}")

            timeline.log('blocked', ref_id=ref_id, ref_type=event_type, recorded_by=recorded_by,
                         status='blocked_deps', blocked_on=missing_deps)
            queues.blocked.add(recorded_id, recorded_by, missing_deps, safedb)
            return [None, recorded_id]

    # All dependencies satisfied - proceed with projection
    projected_id = None
    log.info(f"Projecting event type: {event_type}")
    timeline.log('dispatching', ref_id=ref_id, ref_type=event_type, recorded_by=recorded_by)

    # Check if this event has been marked as deleted (prevents projection of deleted messages)
    # This handles the case where a deletion arrives before or after the message
    if event_type == 'message':
        deleted_check = safedb.query_one(
            "SELECT 1 FROM deleted_events WHERE event_id = ? AND recorded_by = ? LIMIT 1",
            (ref_id, recorded_by)
        )
        if deleted_check:
            log.info(f"Skipping projection of message {ref_id[:20]}... - message is marked as deleted")
            return [None, recorded_id]

    if event_type == 'message':
        projected_id = message.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'message_deletion':
        from events.content import message_deletion
        projected_id = message_deletion.project(ref_id, recorded_by, recorded_at, db)
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
        log.error(f"[CHANNEL_PROJECT_DISPATCHER] Dispatching channel.project() for channel_id={ref_id[:20]}... recorded_by={recorded_by[:20]}...")
        channel.project(ref_id, recorded_by, recorded_at, db)
        projected_id = ref_id
        log.error(f"[CHANNEL_PROJECT_DISPATCHER] ✓ channel.project() completed for channel_id={ref_id[:20]}... recorded_by={recorded_by[:20]}...")
    elif event_type == 'sync':
        from events.transit import sync
        sync.project(ref_id, recorded_by, recorded_at, db)
        projected_id = ref_id
    elif event_type == 'sync_connect':
        from events.transit import sync_connect
        sync_connect.project(ref_id, recorded_by, recorded_at, db)
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
    elif event_type == 'network':
        from events.identity import network
        projected_id = network.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'network_created':
        from events.identity import network_created
        projected_id = network_created.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'network_joined':
        from events.identity import network_joined
        projected_id = network_joined.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'bootstrap_complete':
        from events.identity import bootstrap_complete
        projected_id = bootstrap_complete.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'address':
        from events.identity import address
        projected_id = address.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'file':
        from events.content import file
        file.project(ref_id, event_data, recorded_by, recorded_at, db)
        projected_id = ref_id
    elif event_type == 'file_slice':
        from events.content import file_slice
        file_slice.project(ref_id, event_data, recorded_by, recorded_at, db)
        projected_id = ref_id
    elif event_type == 'message_attachment':
        from events.content import message_attachment
        message_attachment.project(ref_id, event_data, recorded_by, recorded_at, db)
        projected_id = ref_id
    elif event_type == 'network_address':
        from events.network import address as network_address
        projected_id = network_address.project(ref_id, recorded_by, recorded_at, db)
    elif event_type == 'network_intro':
        from events.network import intro as network_intro
        projected_id = network_intro.project(ref_id, recorded_by, recorded_at, db)

    # Mark event as valid for this peer
    log.warning(f"[VALID_EVENT] Marking {event_type} event {ref_id[:20]}... as valid for peer {recorded_by[:20]}...")

    # Check if blob is in store before marking as valid
    unsafedb = create_unsafe_db(db)
    in_store = unsafedb.query_one("SELECT 1 FROM store WHERE id = ?", (ref_id,))
    if not in_store:
        log.error(f"[VALID_EVENT_BUG] ❌ Marking event {ref_id[:20]}... as valid but blob NOT in store! type={event_type}")

    # Log before and after valid_events insert
    already_valid = safedb.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ? LIMIT 1",
        (ref_id, recorded_by)
    )
    if already_valid:
        log.warning(f"[VALID_EVENT_ALREADY] Event {ref_id[:20]}... already in valid_events for peer {recorded_by[:20]}...")

    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (ref_id, recorded_by)
    )

    # Verify insertion
    check_valid = safedb.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ? LIMIT 1",
        (ref_id, recorded_by)
    )
    if check_valid:
        log.warning(f"[VALID_EVENT_SUCCESS] ✓ Event {ref_id[:20]}... is now in valid_events for peer {recorded_by[:20]}...")
    else:
        log.error(f"[VALID_EVENT_FAILED] ✗ Event {ref_id[:20]}... NOT in valid_events after insert! peer={recorded_by[:20]}...")

    # Add to projected_events if event has created_at (for UI lazy loading)
    if event_data and event_data.get('created_at') is not None and event_type:
        safedb.execute(
            """INSERT OR IGNORE INTO projected_events (event_id, event_type, created_at, recorded_by)
               VALUES (?, ?, ?, ?)""",
            (ref_id, event_type, event_data['created_at'], recorded_by)
        )
        log.debug(f"Added {event_type} {ref_id[:20]}... to projected_events with created_at={event_data['created_at']}")

    # Notify blocked queue - unblock events that were waiting for this event
    unblocked_ids = queues.blocked.notify_event_valid(ref_id, recorded_by, safedb)
    if unblocked_ids:
        log.warning(f"Unblocked {len(unblocked_ids)} events after {ref_id[:20]}... became valid for peer {recorded_by[:20]}...")
        # Re-project unblocked events recursively
        project_ids(unblocked_ids, db, _recursion_depth + 1)
    else:
        log.debug(f"No events to unblock after {ref_id[:20]}... for peer {recorded_by[:20]}...")

    # Timeline: Log successful projection completion
    timeline.log('proj_end', ref_id=ref_id, ref_type=event_type, recorded_by=recorded_by, status='success')

    return [projected_id, recorded_id]


def create(ref_id: str, recorded_by: str, t_ms: int, db: Any, return_dupes: bool) -> str:
    """Create a recorded event for the given ref_id and return the recorded_id."""

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
