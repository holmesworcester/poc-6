"""Invite accepted event type (local-only, captures invite acceptance)."""
from typing import Any
import json
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(invite_id: str, invite_prekey_id: str, invite_private_key: bytes,
           peer_id: str, t_ms: int, db: Any) -> str:
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

    Returns:
        invite_accepted_id: Event ID
    """
    log.info(f"invite_accepted.create() for invite={invite_id}, peer={peer_id}")

    event_data = {
        'type': 'invite_accepted',
        'invite_id': invite_id,
        'invite_prekey_id': invite_prekey_id,
        'invite_private_key': crypto.b64encode(invite_private_key),
        'created_by': peer_id,
        'created_at': t_ms
    }

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

    log.info(f"invite_accepted.project() completed for {recorded_by}")
