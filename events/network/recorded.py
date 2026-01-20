"""Recorded event management functions.

ARCHITECTURE NOTE: Event Type Registry
======================================
Each event module declares its own metadata via module-level constants:

    EVENT_TYPE = 'message'
    SHAREABLE = True
    EVENT_SPEC = { ... }  # V2 projector specification

The registry (events/registry.py) auto-discovers these declarations and provides:
    - is_shareable(event_type) - whether event syncs to other peers
    - get_event_spec(event_type) - V2 projector specification
    - get_project_pure_fn(event_type) - pure projector function for v2

Benefits:
    - Adding new event type: just create module with metadata + project_pure()
    - No more forgotten updates to LOCAL_ONLY_TYPES
    - Metadata lives with the code it describes
    - Easy to audit: grep -r "SHAREABLE = True" events/

See docs/planning/event-registry-design.md for full design rationale.
"""
from typing import Any
import json
import logging

from events import registry
from events.group import group
from events.content import message
from core import store
from core import crypto
from core.db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def project_ids(recorded_ids: list[str], db: Any, _recursion_depth: int = 0,
                skip_negentropy: bool = False) -> list[list[str | None]]:
    """Since `recorded` is the event that triggers projection, this is the central function for projection."""
    """It calls the necessary project functions in other modules for the given event types."""

    if _recursion_depth > 100:
        log.error(f"[PROJECT_IDS] RECURSION LIMIT EXCEEDED depth={_recursion_depth} - possible infinite loop!")
        return []

    log.info(f"recorded.project_ids() projecting {len(recorded_ids)} recorded events (depth={_recursion_depth}, skip_neg={skip_negentropy})")
    projected_ids = []
    for recorded_id in recorded_ids:
        try:
            result = project(recorded_id, db, _recursion_depth, skip_negentropy=skip_negentropy)
            projected_ids.append(result)
        except Exception as e:
            log.error(f"[PROJECT_IDS_EXCEPTION] ❌ EXCEPTION projecting recorded_id={recorded_id[:20]}... depth={_recursion_depth}: {str(e)[:200]}")
            import traceback
            traceback.print_exc()
            raise  # Re-raise to fail immediately so we can see the error
    log.info(f"recorded.project_ids() completed projection of {len(recorded_ids)} events (depth={_recursion_depth})")
    return projected_ids


def project(recorded_id: str, db: Any, _recursion_depth: int = 0, _triggered_by: str = 'initial',
            skip_negentropy: bool = False) -> list[str | None]:
    """Project recorded event with two-phase dependency checking.

    1. Check encryption keys (block if missing).
    2. Check event dependencies (block if missing).
    Dispatches to type-specific projector if all deps satisfied.

    Args:
        _triggered_by: What triggered this projection (for debugging causality)
        skip_negentropy: If True, skip negentropy bucket updates (caller will batch them)
    """
    from events.identity import peer
    from events.content import channel
    from core import queues
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

    # Try to unwrap (for encrypted events)
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

    # Mark shareable events for sync (centralized marking via registry)
    # This happens BEFORE blocking so blocked events (crypto or semantic deps) are still shareable
    # Track that this peer recorded this event and can share it (not who created it)
    # Note: 'recorded' type is always local-only (not in registry)

    should_mark_shareable = False
    if event_type:
        # Use registry to check if shareable (defaults to False for unknown types)
        should_mark_shareable = registry.is_shareable(event_type)
    elif missing_key_ids:
        # Encrypted blob we can't decrypt - but local events are never encrypted!
        # So this must be shareable
        should_mark_shareable = True

    if should_mark_shareable:
        # Mark event as shareable in shareable_events table
        # Always use created_at=None for simplicity and determinism
        # Sync protocol doesn't need created_at - it uses recorded_at for ordering
        # UI lazy loading will use separate projected_events table with created_at
        from events.network import negentropy
        log.debug(f"Adding {event_type or 'unknown'} {ref_id[:20]}... to shareable_events with created_at=None (skip_neg={skip_negentropy})")
        negentropy.add_shareable_event(
            ref_id,
            recorded_by,
            created_at=None,  # Always None for determinism (encrypted events don't have created_at)
            recorded_at=recorded_at,
            db=db,
            skip_negentropy=skip_negentropy
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

    event_type = event_data.get('type')
    projected_id = None

    # TRUST ANCHOR ENFORCEMENT for network events
    # Trust anchors are inserted by invite_accepted.project() for ALL cases:
    # - Creator: self-invites via peer_shared.join() → invite_accepted → trust anchor
    # - Joiner: accepts invite → invite_accepted → trust anchor
    # This is a uniform model - no special case for "creator detection".
    if event_type == 'network':
        trust_anchor = safedb.query_one(
            "SELECT 1 FROM trust_anchors WHERE network_id = ? AND recorded_by = ?",
            (ref_id, recorded_by)
        )
        existing = safedb.query_one(
            "SELECT 1 FROM networks WHERE network_id = ? AND recorded_by = ?",
            (ref_id, recorded_by)
        )
        already_valid = safedb.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (ref_id, recorded_by)
        )
        if not trust_anchor and not existing and not already_valid:
            log.warning(f"[TRUST_ANCHOR] Rejecting network {ref_id[:20]}... - not trusted by {recorded_by[:20]}...")
            return [None, recorded_id]

    project_pure_fn = registry.get_project_pure_fn(event_type) if event_type else None
    event_spec = registry.get_event_spec(event_type) if event_type else None

    if event_spec and project_pure_fn:
        from core.projection_v2 import resolver as v2_resolver
        from core.projection_v2 import apply as v2_apply

        resolve_result = v2_resolver.resolve_event(
            ref_id=ref_id,
            event_type=event_type,
            event_data=event_data,
            recorded_by=recorded_by,
            recorded_at=recorded_at,
            db=db,
        )

        if resolve_result.status == 'block':
            missing_deps = list(resolve_result.missing)

            requester_peer_shared_id = event_data.get('peer_shared_id', 'N/A')
            log.warning(
                f"Blocking {event_type} event {ref_id[:20]}... recorded_by={recorded_by[:20]}... "
                f"requester_peer_shared={requester_peer_shared_id[:20]}... missing deps: "
                f"{[d[:20] for d in missing_deps]}"
            )

            if event_type == 'channel':
                log.error(
                    f"[CHANNEL_BLOCKED] channel_id={ref_id[:20]}... recorded_by={recorded_by[:20]}... "
                    f"missing_deps={missing_deps}"
                )

            timeline.log(
                'blocked',
                ref_id=ref_id,
                ref_type=event_type,
                recorded_by=recorded_by,
                status='blocked_deps',
                blocked_on=missing_deps,
            )
            queues.blocked.add(recorded_id, recorded_by, missing_deps, safedb)
            return [None, recorded_id]

        if resolve_result.status == 'reject':
            log.warning(
                f"[PROJECTION_FAILED] Event {event_type} {ref_id[:20]}... rejected: {resolve_result.error}"
            )
            timeline.log('proj_end', ref_id=ref_id, ref_type=event_type, recorded_by=recorded_by, status='failed')
            return [None, recorded_id]

        if resolve_result.status != 'ok' or resolve_result.ctx is None:
            log.error(
                f"[PROJECTION_FAILED] Event {event_type} {ref_id[:20]}... resolve returned {resolve_result.status}"
            )
            timeline.log('proj_end', ref_id=ref_id, ref_type=event_type, recorded_by=recorded_by, status='failed')
            return [None, recorded_id]

        log.warning(
            f"[PROJECTION_DISPATCH] (v2) Projecting event type: {event_type}, "
            f"ref_id={ref_id[:20]}..., recorded_by={recorded_by[:20]}..."
        )
        timeline.log('dispatching', ref_id=ref_id, ref_type=event_type, recorded_by=recorded_by)

        if event_type == 'message':
            deleted_check = safedb.query_one(
                "SELECT 1 FROM deleted_events WHERE event_id = ? AND recorded_by = ? LIMIT 1",
                (ref_id, recorded_by)
            )
            if deleted_check:
                log.info(f"Skipping projection of message {ref_id[:20]}... - message is marked as deleted")
                return [None, recorded_id]

        try:
            projector_result = project_pure_fn(resolve_result.ctx)
        except Exception as e:
            log.error(f"[PROJECTION_FAILED] project_pure raised for {ref_id[:20]}...: {e}")
            timeline.log('proj_end', ref_id=ref_id, ref_type=event_type, recorded_by=recorded_by, status='failed')
            return [None, recorded_id]

        try:
            v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)
        except Exception as e:
            log.error(f"[PROJECTION_FAILED] apply_writes raised for {ref_id[:20]}...: {e}")
            timeline.log('proj_end', ref_id=ref_id, ref_type=event_type, recorded_by=recorded_by, status='failed')
            return [None, recorded_id]

        if not projector_result.valid_event:
            log.warning(
                f"[PROJECTION_FAILED] Event {event_type} {ref_id[:20]}... failed projection - NOT marking as valid"
            )
            timeline.log('proj_end', ref_id=ref_id, ref_type=event_type, recorded_by=recorded_by, status='failed')
            return [None, recorded_id]

        if event_type == 'group':
            # Legacy group.project triggers pending name retries; keep behavior during v2 migration.
            from events.group.group_key_shared import retry_pending_name_updates

            retry_pending_name_updates(recorded_by, db)

        if event_type == 'peer_removed':
            # v2 projector writes to removed_peers table, but we also need side effects:
            # 1. Delete all connections with the removed peer
            # 2. Rotate group keys if this was a user's peer
            from events.network import connection_request
            from events.identity import peer_removed as peer_removed_module

            removed_peer_shared_id = event_data.get('removed_peer_shared_id')
            if removed_peer_shared_id:
                # Delete connections
                deleted_count = connection_request.remove_connections_for_peer(removed_peer_shared_id, db)
                log.info(f"[PEER_REMOVED_V2] deleted {deleted_count} connection(s) for removed peer {removed_peer_shared_id[:20]}...")

                # Rotate group keys - lookup user_id for the removed peer
                peer_row = safedb.query_one(
                    "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
                    (removed_peer_shared_id, recorded_by)
                )
                if peer_row and peer_row.get('user_id'):
                    removed_user_id = peer_row['user_id']
                    log.info(f"[PEER_REMOVED_V2] rotating keys for user {removed_user_id[:20]}...")
                    peer_removed_module._rotate_keys_for_removed_peer_user(removed_user_id, recorded_by, recorded_at, db)

        if event_type == 'user_removed':
            # v2 projector writes to removed_users table, but we also need side effects:
            # 1. Cascade: Mark all peers of the removed user as removed
            # 2. Delete connections for each peer
            # 3. Rotate group keys (only if this peer created the event)
            from events.identity import user_removed as user_removed_module

            removed_user_id = event_data.get('removed_user_id')
            removed_at = event_data.get('created_at')
            removed_by = event_data.get('removed_by')

            if removed_user_id:
                user_removed_module._handle_user_removed_side_effects(
                    removed_user_id, removed_at, removed_by, recorded_by, db
                )

        if event_type == 'invite_accepted':
            # v2 projector writes to invite_accepteds table, but we also need to:
            # 1. Store network_id in trust_anchors (NOT valid_events - network validates when it arrives)
            # 2. Store inviter's peer_shared blob from link data
            import base64

            invite_link_data = event_data.get('invite_link_data', {})
            network_id = invite_link_data.get('network_id')
            inviter_peer_shared_id = invite_link_data.get('inviter_peer_shared_id')
            inviter_peer_shared_blob_b64 = invite_link_data.get('inviter_peer_shared_blob')

            if network_id:
                # Store in trust_anchors - network will validate when it arrives via sync
                # Network is the ONLY trust anchor - invites are NEVER force-validated
                safedb.execute(
                    "INSERT OR IGNORE INTO trust_anchors (network_id, recorded_by, created_at) VALUES (?, ?, ?)",
                    (network_id, recorded_by, recorded_at)
                )
                log.warning(f"[INVITE_ACCEPTED_V2] stored network_id={network_id[:20]}... in trust_anchors")

                # Uniform trust anchor model: Now that trust anchor exists, project network if blob is in store
                # This handles both creator (network.create() stored blob) and joiner (sync delivered blob)
                from events.identity import network as network_module
                network_blob = store.get(network_id, unsafedb)
                if network_blob:
                    # Check if already projected
                    already_projected = safedb.query_one(
                        "SELECT 1 FROM networks WHERE network_id = ? AND recorded_by = ?",
                        (network_id, recorded_by)
                    )
                    if not already_projected:
                        result = network_module.project(network_id, recorded_by, recorded_at, db)
                        if result:
                            log.warning(f"[INVITE_ACCEPTED_V2] projected network event {network_id[:20]}...")
                            # Mark network as valid
                            safedb.execute(
                                "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
                                (network_id, recorded_by)
                            )
                            # CRITICAL: Notify blocked queue to unblock events waiting on network
                            # This triggers the cascade: network -> invite -> user -> peer_shared
                            unblocked_by_network = queues.blocked.notify_event_valid(network_id, recorded_by, safedb)
                            if unblocked_by_network:
                                log.warning(f"[INVITE_ACCEPTED_V2] unblocked {len(unblocked_by_network)} events after network became valid")
                                # Re-project unblocked events recursively
                                project_ids(unblocked_by_network, db)
                        else:
                            log.warning(f"[INVITE_ACCEPTED_V2] network projection failed for {network_id[:20]}...")
                    else:
                        log.warning(f"[INVITE_ACCEPTED_V2] network already projected {network_id[:20]}...")
            else:
                # No network_id in invite_link_data - this should not happen anymore
                # All invite types (user and peer) should include network_id for proper trust anchoring
                # Events will BLOCK until dependencies (network -> invite -> peer_shared) arrive via sync
                log.warning(f"[INVITE_ACCEPTED_V2] missing network_id in invite_link_data - events will block until deps sync, keys={list(invite_link_data.keys())}")

            # Store inviter's peer_shared blob from link data (for sync)
            if inviter_peer_shared_blob_b64 and inviter_peer_shared_id:
                padding = 4 - len(inviter_peer_shared_blob_b64) % 4
                if padding != 4:
                    inviter_peer_shared_blob_b64 += '=' * padding
                inviter_peer_shared_blob = base64.urlsafe_b64decode(inviter_peer_shared_blob_b64)

                stored_ps_id = store.blob(inviter_peer_shared_blob, recorded_at, True, unsafedb)
                log.warning(f"[INVITE_ACCEPTED_V2] stored inviter peer_shared blob, id={stored_ps_id[:20]}...")

                # Create recorded wrapper for inviter's peer_shared so it can be projected
                ps_recorded_id = create(stored_ps_id, recorded_by, recorded_at, db, return_dupes=False)
                log.warning(f"[INVITE_ACCEPTED_V2] created recorded wrapper for inviter peer_shared")

        projected_id = ref_id

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

    from core.db import create_safe_db
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
