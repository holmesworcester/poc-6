"""Recorded event management functions."""
from typing import Any
import json
import logging

import store
import crypto
from db import create_safe_db, create_unsafe_db
from projectors import dispatch, check_deps

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
            result = project_event(recorded_id, db, _recursion_depth)
            projected_ids.append(result)
        except Exception as e:
            log.error(f"[PROJECT_IDS_EXCEPTION] ❌ EXCEPTION projecting recorded_id={recorded_id[:20]}... depth={_recursion_depth}: {str(e)[:200]}")
            import traceback
            traceback.print_exc()
            raise  # Re-raise to fail immediately so we can see the error
    log.info(f"recorded.project_ids() completed projection of {len(recorded_ids)} events (depth={_recursion_depth})")
    return projected_ids


def project_event(recorded_id: str, db: Any, _recursion_depth: int = 0, _triggered_by: str = 'initial') -> list[str | None]:
    """Project recorded event with two-phase dependency checking.

    Phase 1: Check encryption keys (block if missing).
    Phase 2: Check event dependencies (block if missing).
    Dispatches to type-specific projector if all deps satisfied.

    Args:
        _triggered_by: What triggered this projection (for debugging causality)
    """
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
    LOCAL_ONLY_TYPES = {'peer', 'transit_key', 'group_key', 'transit_prekey', 'group_prekey', 'recorded', 'network_joined', 'invite_accepted', 'sync_connect', 'purge_expired'}
    # Note: bootstrap_complete removed in Phase 3, network_created removed in Phase 5 (no longer created, but can still project for backward compat)
    # Note: purge_expired is local-only because each peer independently purges their own expired events

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
        from events.network import sync
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

    # Phase 2: User events now have signed_by=invite_id and user_pubkey (not invite_pubkey)
    if event_type == 'user' and 'signed_by' in event_data:
        # Self-created user with invite proof: creator doesn't have invite in valid_events
        # (invite comes from out-of-band link, not network sync)
        # Note: signed_by is invite_id for Phase 2 user events, check peer_id from event
        # User events have peer_id field which is the peer_shared_id
        user_peer_shared_id = event_data.get('peer_id')
        # Check if this user's peer_id is THIS peer's peer_shared_id
        peer_self_row = safedb.query_one(
            "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
            (recorded_by, recorded_by)
        )
        if peer_self_row and peer_self_row['peer_shared_id'] == user_peer_shared_id:
            skip_dep_check = True
            log.info(f"[USER_SKIP_DEPS] Skipping dep check for self-created user event")

    # NOTE: Invite validation moved to invite.project() for better modularity
    # Invites from URLs (with invite_accepted) skip validation in projector
    # Invites from sync are validated (signature, network, creator) in projector
    # NOTE: invite_accepted is now in NO_DEPS_TYPES in check_deps() since it's local-only

    if not skip_dep_check:
        missing_deps = check_deps(event_data, recorded_by, db)
        if missing_deps:
            # Ephemeral events (transit-layer) are recurring/retryable
            # If deps are missing, drop them - sender will retry
            EPHEMERAL_TYPES = {'sync_connect', 'sync_request', 'sync_response', 'purge_expired'}
            if event_type in EPHEMERAL_TYPES:
                log.warning(f"[EPHEMERAL_DROP] Dropping ephemeral {event_type} event {ref_id[:20]}... with missing deps: {[d[:20] for d in missing_deps]}")
                return [None, recorded_id]

            # Historical events - block until dependencies resolved
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
    log.warning(f"[PROJECTION_DISPATCH] Projecting event type: {event_type}, ref_id={ref_id[:20]}..., recorded_by={recorded_by[:20]}...")
    timeline.log('dispatching', ref_id=ref_id, ref_type=event_type, recorded_by=recorded_by)

    # Check if this event has been marked as deleted (prevents projection of deleted messages)
    if event_type == 'message':
        deleted_check = safedb.query_one(
            "SELECT 1 FROM deleted_events WHERE event_id = ? AND recorded_by = ? LIMIT 1",
            (ref_id, recorded_by)
        )
        if deleted_check:
            log.info(f"Skipping projection of message {ref_id[:20]}... - message is marked as deleted")
            return [None, recorded_id]

    # Dispatch to projector
    projected_id = dispatch(event_type, ref_id, recorded_by, recorded_at, db, event_data)

    # Special case: group_key_shared returns None when event is not for us (can't decrypt)
    # Still shareable, but don't mark as valid
    if event_type == 'group_key_shared' and projected_id is None:
        log.info(f"group_key_shared projection returned None (not for us), skipping valid marking")
        return [None, recorded_id]

    # Only mark event as valid if projection succeeded (projected_id is not None)
    # Events that fail projection (authorization, missing data) should NOT be marked valid
    # They will be retried later when dependencies are satisfied
    if projected_id is None:
        log.warning(f"[PROJECTION_FAILED] Event {event_type} {ref_id[:20]}... failed projection - NOT marking as valid")
        timeline.log('proj_end', ref_id=ref_id, ref_type=event_type, recorded_by=recorded_by, status='failed')
        return [None, recorded_id]

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
        # Clean up successfully projected events from blocked queue
        _cleanup_successfully_projected_events(unblocked_ids, recorded_by, db)
    else:
        log.debug(f"No events to unblock after {ref_id[:20]}... for peer {recorded_by[:20]}...")

    # Timeline: Log successful projection completion
    timeline.log('proj_end', ref_id=ref_id, ref_type=event_type, recorded_by=recorded_by, status='success')

    return [projected_id, recorded_id]


def _cleanup_successfully_projected_events(unblocked_ids: list[str], recorded_by: str, db: Any) -> int:
    """Delete events from blocked_events_ephemeral after successful projection.

    This function implements the cleanup logic that was previously only in test utilities.
    Events with deps_remaining=0 should be removed from the blocked queue AFTER confirming
    they successfully projected (i.e., their ref_id is in valid_events).

    Args:
        unblocked_ids: List of recorded_ids that were just unblocked and re-projected
        recorded_by: The peer who projected them
        db: Database connection

    Returns:
        Number of events cleaned up from blocked_events_ephemeral
    """
    if not unblocked_ids:
        return 0

    from db import create_safe_db
    safedb = create_safe_db(db, recorded_by=recorded_by)
    cleaned_count = 0

    for recorded_id in unblocked_ids:
        try:
            # Get the ref_id from this recorded event
            recorded_blob = store.get(recorded_id, db)
            if not recorded_blob:
                continue

            recorded_data = crypto.parse_json(recorded_blob)
            ref_id = recorded_data.get('ref_id')
            if not ref_id:
                continue

            # Check if the event successfully projected (is in valid_events)
            is_valid = safedb.query_one(
                "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ? LIMIT 1",
                (ref_id, recorded_by)
            )

            if is_valid:
                # Safe to delete - the event was successfully projected
                safedb.execute(
                    "DELETE FROM blocked_events_ephemeral WHERE recorded_id = ? AND recorded_by = ?",
                    (recorded_id, recorded_by)
                )
                # Also clean up deps table entries
                safedb.execute(
                    "DELETE FROM blocked_event_deps_ephemeral WHERE recorded_id = ? AND recorded_by = ?",
                    (recorded_id, recorded_by)
                )
                cleaned_count += 1
            else:
                # Event projection failed or hasn't completed - keep it blocked for retry
                log.debug(f"_cleanup_successfully_projected_events: event {recorded_id[:20]}... not in valid_events, keeping blocked")
        except Exception as e:
            log.warning(f"_cleanup_successfully_projected_events: error processing {recorded_id[:20]}...: {e}")
            continue

    if cleaned_count > 0:
        log.info(f"_cleanup_successfully_projected_events: cleaned up {cleaned_count}/{len(unblocked_ids)} events from blocked queue")

    return cleaned_count


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
