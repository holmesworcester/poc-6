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
    from core import queues

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

    log.debug(f"recorded.project(): ref_id={ref_id[:20]}..., recorded_by={recorded_by[:20]}...")

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

    # Parse event data to determine type (needed for shareable check)
    event_data = None
    event_type = None

    if plaintext:
        # Successfully decrypted or was plaintext
        event_data = crypto.parse_json(plaintext)
        event_type = event_data.get('type')
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

    # Check if event is pre-deleted (deletion arrived before the event itself)
    # Must check BEFORE marking shareable - deleted events should never sync
    if event_type:
        event_spec_early = registry.get_event_spec(event_type)
        if event_spec_early and event_spec_early.get('skip_if_deleted'):
            deleted_check = safedb.query_one(
                "SELECT 1 FROM deleted_events WHERE event_id = ? AND recorded_by = ? LIMIT 1",
                (ref_id, recorded_by)
            )
            if deleted_check:
                # Event was deleted - clean up completely, never sync
                log.debug(f"Event {event_type} {ref_id[:20]}... is deleted - purging")
                # Remove from shareable_events (in case it was somehow added)
                safedb.execute(
                    "DELETE FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
                    (ref_id, recorded_by)
                )
                # Delete the blob from store
                unsafedb.execute("DELETE FROM store WHERE id = ?", (ref_id,))
                return [None, recorded_id]

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
        queues.blocked.add(recorded_id, recorded_by, missing_key_ids, safedb)
        return [None, recorded_id]

    # If we got here without event_data, return early
    if not event_data:
        return [None, recorded_id]

    event_type = event_data.get('type')
    projected_id = None

    # Trust anchor enforcement moved to resolver via EVENT_SPEC['trust_anchor']

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

            queues.blocked.add(recorded_id, recorded_by, missing_deps, safedb)
            return [None, recorded_id]

        if resolve_result.status == 'reject':
            log.warning(
                f"[PROJECTION_FAILED] Event {event_type} {ref_id[:20]}... rejected: {resolve_result.error}"
            )
            return [None, recorded_id]

        if resolve_result.status != 'ok' or resolve_result.ctx is None:
            log.error(
                f"[PROJECTION_FAILED] Event {event_type} {ref_id[:20]}... resolve returned {resolve_result.status}"
            )
            return [None, recorded_id]

        log.debug(
            f"[PROJECTION_DISPATCH] (v2) Projecting event type: {event_type}, "
            f"ref_id={ref_id[:20]}..., recorded_by={recorded_by[:20]}..."
        )

        try:
            projector_result = project_pure_fn(resolve_result.ctx)
        except Exception as e:
            log.error(f"[PROJECTION_FAILED] project_pure raised for {ref_id[:20]}...: {e}")
            return [None, recorded_id]

        try:
            v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)
        except Exception as e:
            log.error(f"[PROJECTION_FAILED] apply_writes raised for {ref_id[:20]}...: {e}")
            return [None, recorded_id]

        if not projector_result.valid_event:
            log.warning(
                f"[PROJECTION_FAILED] Event {event_type} {ref_id[:20]}... failed projection - NOT marking as valid"
            )
            return [None, recorded_id]

        projected_id = ref_id

    # Only mark event as valid if projection succeeded (projected_id is not None)
    # Events that fail projection (authorization, missing data) should NOT be marked valid
    # They will be retried later when dependencies are satisfied
    if projected_id is None:
        log.warning(f"[PROJECTION_FAILED] Event {event_type} {ref_id[:20]}... failed projection - NOT marking as valid")
        return [None, recorded_id]

    # Mark event as valid for this peer
    log.debug(f"Marking {event_type} event {ref_id[:20]}... as valid for peer {recorded_by[:20]}...")

    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (ref_id, recorded_by)
    )

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
        log.debug(f"Unblocked {len(unblocked_ids)} events after {ref_id[:20]}... became valid")
        # Re-project unblocked events recursively
        project_ids(unblocked_ids, db, _recursion_depth + 1)
        # Clean up successfully projected events from blocked queue
        _cleanup_successfully_projected_events(unblocked_ids, recorded_by, db)

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

    log.debug(f"recorded.create(): ref_id={ref_id[:20]}..., recorded_by={recorded_by[:20]}...")

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
