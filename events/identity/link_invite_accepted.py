"""Link invite accepted event type (local-only, captures link acceptance)."""
from typing import Any
import json
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(link_invite_id: str, link_prekey_id: str, link_private_key: bytes,
           peer_id: str, t_ms: int, db: Any) -> str:
    """Create local link_invite_accepted event (not shareable).

    This event captures the link acceptance action and stores ALL
    out-of-band data from the link URL for event-sourcing (reprojection).

    Args:
        link_invite_id: The link_invite event being accepted
        link_prekey_id: Deterministic prekey ID for storing link proof keypair
        link_private_key: Private key for GKS decryption + link proof signature
        peer_id: New device's peer_id (local)
        t_ms: Timestamp
        db: Database connection

    Returns:
        link_invite_accepted_id: Event ID
    """
    log.info(f"link_invite_accepted.create() for link_invite={link_invite_id}, peer={peer_id}")

    event_data = {
        'type': 'link_invite_accepted',
        'link_invite_id': link_invite_id,
        'link_prekey_id': link_prekey_id,
        'link_private_key': crypto.b64encode(link_private_key),
        'created_by': peer_id,
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # Store with recorded wrapper and projection
    link_invite_accepted_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"link_invite_accepted.create() created link_invite_accepted_id={link_invite_accepted_id}")
    return link_invite_accepted_id


def project(link_invite_accepted_id: str, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Project link_invite_accepted: restore link link_invite data for event-sourcing."""
    log.warning(f"[LINK_INVITE_ACCEPTED_PROJECT_ENTRY] id={link_invite_accepted_id[:20]}..., recorded_by={recorded_by[:20]}...")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(link_invite_accepted_id, unsafedb)
    if not blob:
        log.warning(f"link_invite_accepted.project() blob not found")
        return

    event_data = crypto.parse_json(blob)

    # Extract link proof keypair data
    link_invite_id = event_data['link_invite_id']
    link_prekey_id = event_data['link_prekey_id']  # From link URL
    link_private_key = crypto.b64decode(event_data['link_private_key'])

    # Get link_invite event to extract public key
    link_invite_blob = store.get(link_invite_id, unsafedb)
    if not link_invite_blob:
        log.warning(f"link_invite_accepted.project() link_invite blob not found: {link_invite_id}")
        return

    link_invite_event = crypto.parse_json(link_invite_blob)

    # Handle both unified invite format (invite_pubkey) and legacy (link_pubkey)
    is_unified_invite = (link_invite_event.get('type') == 'invite' and
                         link_invite_event.get('mode') == 'link')

    if is_unified_invite:
        # Unified invite format: use invite_pubkey
        link_public_key = crypto.b64decode(link_invite_event['invite_pubkey'])
        log.info(f"link_invite_accepted.project() using unified invite format (invite_pubkey)")
    else:
        # Legacy link_invite format: use link_pubkey
        link_public_key = crypto.b64decode(link_invite_event['link_pubkey'])
        log.info(f"link_invite_accepted.project() using legacy link_invite format (link_pubkey)")

    # Store link proof keypair in group_prekeys table (for GKS decryption)
    safedb.execute(
        """INSERT OR IGNORE INTO group_prekeys
           (prekey_id, owner_peer_id, public_key, private_key, created_at, recorded_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            link_prekey_id,  # Deterministic prekey ID (matches GKS hint)
            recorded_by,
            link_public_key,
            link_private_key,
            event_data['created_at'],
            recorded_by
        )
    )

    log.warning(f"[LINK_INVITE_ACCEPTED_PROJECT] stored link_private_key prekey_id={link_prekey_id[:20]}... for peer {recorded_by[:20]}...")

    # Unblock events that were waiting for this prekey (e.g., group_key_shared events)
    import queues
    from events.transit import recorded as recorded_module
    unblocked_ids = queues.blocked.notify_event_valid(link_prekey_id, recorded_by, safedb)
    if unblocked_ids:
        log.info(f"link_invite_accepted.project() unblocked {len(unblocked_ids)} events waiting for link prekey")
        recorded_module.project_ids(unblocked_ids, db)

    # Mark link_invite_accepted as valid
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (link_invite_accepted_id, recorded_by)
    )

    # Mark the link_invite itself as valid (restores out-of-band trust from link URL)
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (link_invite_id, recorded_by)
    )

    log.info(f"link_invite_accepted.project() completed for {recorded_by}")
