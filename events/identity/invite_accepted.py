"""Invite accepted event type (local-only, captures invite acceptance)."""
from typing import Any
import json
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(invite_id: str, invite_prekey_id: str, invite_private_key: bytes,
           peer_id: str, t_ms: int, db: Any, first_peer: str | None = None) -> str:
    """Create local invite_accepted event (not shareable).

    This event captures the invite acceptance action and stores ALL
    out-of-band data from the invite link for event-sourcing (reprojection).

    Args:
        invite_id: The invite event being accepted
        invite_prekey_id: Deterministic prekey ID for storing invite proof keypair
        invite_private_key: Private key for GKS decryption + invite proof signature
        peer_id: Bob's peer_id (local)
        t_ms: Timestamp
        db: Database connection
        first_peer: Optional peer_shared_id of network creator (for self-bootstrapping)

    Returns:
        invite_accepted_id: Event ID
    """
    log.info(f"invite_accepted.create() for invite={invite_id}, peer={peer_id}, first_peer={first_peer[:20] if first_peer else 'None'}...")

    event_data = {
        'type': 'invite_accepted',
        'invite_id': invite_id,
        'invite_prekey_id': invite_prekey_id,
        'invite_private_key': crypto.b64encode(invite_private_key),
        'created_by': peer_id,
        'created_at': t_ms
    }

    # Phase 5: Store first_peer for self-bootstrapping
    if first_peer:
        event_data['first_peer'] = first_peer

    blob = json.dumps(event_data).encode()

    # Store with recorded wrapper and projection
    invite_accepted_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"invite_accepted.create() created invite_accepted_id={invite_accepted_id}")
    return invite_accepted_id


def project(invite_accepted_id: str, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Project invite_accepted: restore ALL invite link data for event-sourcing.

    This restores the invite_transit_key from the invite link and enables
    full reprojection without the original invite link.
    """
    log.warning(f"[INVITE_ACCEPTED_PROJECT_ENTRY] id={invite_accepted_id[:20]}..., recorded_by={recorded_by[:20]}...")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(invite_accepted_id, unsafedb)
    if not blob:
        log.warning(f"invite_accepted.project() blob not found")
        return

    event_data = crypto.parse_json(blob)

    # Extract invite proof keypair data from invite_accepted and invite events
    invite_id = event_data['invite_id']
    invite_prekey_id = event_data['invite_prekey_id']  # From invite link
    invite_private_key = crypto.b64decode(event_data['invite_private_key'])

    # Get invite event to extract public key
    invite_blob = store.get(invite_id, unsafedb)
    if not invite_blob:
        log.warning(f"invite_accepted.project() invite blob not found: {invite_id}")
        return

    invite_event = crypto.parse_json(invite_blob)
    log.warning(f"[DEBUG_INVITE_EVENT] keys={list(invite_event.keys())}, first_peer={invite_event.get('first_peer', 'MISSING')[:20] if invite_event.get('first_peer') else 'MISSING'}")
    invite_public_key = crypto.b64decode(invite_event['invite_pubkey'])

    # Store invite proof keypair in group_prekeys table (for GKS decryption)
    # Use invite_prekey_id as prekey_id (matches hint in GKS blob)
    safedb.execute(
        """INSERT OR IGNORE INTO group_prekeys
           (prekey_id, owner_peer_id, public_key, private_key, created_at, recorded_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            invite_prekey_id,  # Deterministic prekey ID (matches GKS hint)
            recorded_by,
            invite_public_key,
            invite_private_key,
            event_data['created_at'],
            recorded_by
        )
    )

    log.warning(f"[INVITE_ACCEPTED_PROJECT] stored invite_private_key prekey_id={invite_prekey_id[:20]}... for peer {recorded_by[:20]}...")

    # Unblock events that were waiting for this prekey (e.g., group_key_shared events sealed to this invite)
    import queues
    from events.transit import recorded as recorded_module
    unblocked_ids = queues.blocked.notify_event_valid(invite_prekey_id, recorded_by, safedb)
    if unblocked_ids:
        log.info(f"invite_accepted.project() unblocked {len(unblocked_ids)} events waiting for invite prekey")
        recorded_module.project_ids(unblocked_ids, db)

    # Mark invite_accepted as valid
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (invite_accepted_id, recorded_by)
    )

    # Mark the invite itself as valid (restores out-of-band trust from invite link)
    # This is necessary for reprojection since the invite link is not available
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (invite_id, recorded_by)
    )

    # Store inviter metadata in invite_accepteds table BEFORE bootstrap check
    # This table entry signals that bootstrap has been initiated
    inviter_peer_shared_id = invite_event.get('created_by')
    inviter_transit_prekey_id = invite_event.get('inviter_transit_prekey_id')
    inviter_transit_prekey_public_key = None

    if inviter_transit_prekey_id:
        inviter_transit_prekey_public_key = crypto.b64decode(
            invite_event.get('inviter_transit_prekey_public_key', '')
        )

    safedb.execute("""
        INSERT OR IGNORE INTO invite_accepteds
        (invite_id, inviter_peer_shared_id, address, port,
         inviter_transit_prekey_id, inviter_transit_prekey_public_key,
         created_at, recorded_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        invite_id,
        inviter_peer_shared_id,
        None,  # address - TODO: extract from invite link
        None,  # port - TODO: extract from invite link
        inviter_transit_prekey_id,
        inviter_transit_prekey_public_key,
        event_data['created_at'],
        recorded_by
    ))

    # Phase 5 BOOTSTRAP: Check if this is the first_peer BEFORE unblocking invite dependents
    # We need to unblock __BOOTSTRAP_FIRST_PEER__ first, otherwise dependent events will still be blocked
    first_peer = invite_event.get('first_peer')
    is_first_peer = False

    if first_peer:
        # Get this peer's peer_shared_id to compare with first_peer
        peer_self_row = safedb.query_one(
            "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
            (recorded_by, recorded_by)
        )
        my_peer_shared_id = peer_self_row['peer_shared_id'] if peer_self_row else None
        is_first_peer = (my_peer_shared_id == first_peer)
        log.warning(f"invite_accepted.project() BOOTSTRAP CHECK: first_peer={first_peer[:20] if first_peer else 'None'}..., my_peer_shared_id={my_peer_shared_id[:20] if my_peer_shared_id else 'None'}..., match={is_first_peer}")

    if is_first_peer:
        log.info(f"invite_accepted.project() first_peer detected: recorded_by={recorded_by[:20]}... == first_peer={first_peer[:20]}...")

        # Check if we've already done the bootstrap unblock (idempotency)
        # Look for any events with deps_remaining > 0 waiting for __BOOTSTRAP_FIRST_PEER__
        pending_bootstrap = safedb.query_one("""
            SELECT 1 FROM blocked_event_deps_ephemeral bed
            JOIN blocked_events_ephemeral be ON bed.recorded_id = be.recorded_id
                AND bed.recorded_by = be.recorded_by
            WHERE bed.dep_id = ? AND bed.recorded_by = ? AND be.deps_remaining > 0
            LIMIT 1
        """, ('__BOOTSTRAP_FIRST_PEER__', recorded_by))

        if pending_bootstrap:
            log.warning(f"[BOOTSTRAP_CASCADE] Found pending bootstrap events, triggering unblock")
            # Unblock the bootstrap marker FIRST before processing invite dependents
            unblocked_by_bootstrap = queues.blocked.notify_event_valid('__BOOTSTRAP_FIRST_PEER__', recorded_by, safedb)
            log.warning(f"[BOOTSTRAP_CASCADE] Unblocked bootstrap marker, found {len(unblocked_by_bootstrap)} events: {[eid[:20] for eid in unblocked_by_bootstrap]}")
            if unblocked_by_bootstrap:
                # Re-project the invite (which was artificially blocked)
                recorded_module.project_ids(unblocked_by_bootstrap, db)
                log.warning(f"[BOOTSTRAP_CASCADE] Re-projected bootstrap-blocked events")
        else:
            log.warning(f"[BOOTSTRAP_CASCADE] No pending bootstrap events (already unblocked)")

    # NOW unblock events waiting for the invite (they should no longer be blocked on __BOOTSTRAP_FIRST_PEER__)
    unblocked_by_invite = queues.blocked.notify_event_valid(invite_id, recorded_by, safedb)
    if unblocked_by_invite:
        log.info(f"invite_accepted.project() unblocked {len(unblocked_by_invite)} events waiting for invite")
        recorded_module.project_ids(unblocked_by_invite, db)

    # Phase 5: Admin privileges for first_peer are now granted by user.project()
    # (Moved there because user.project() runs after user is inserted, avoiding race condition)

    log.info(f"invite_accepted.project() completed for {recorded_by}")
