"""Network joined event type - marks successful bootstrap with inviter."""
from typing import Any
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(peer_id: str, peer_shared_id: str, inviter_peer_shared_id: str,
           t_ms: int, db: Any) -> str:
    """Create network_joined event after successful bootstrap.

    Args:
        peer_id: Joiner's peer_id
        peer_shared_id: Joiner's peer_shared_id
        inviter_peer_shared_id: Inviter's peer_shared_id
        t_ms: Timestamp
        db: Database connection

    Returns:
        network_joined event ID
    """
    from events.identity import peer

    # Create event data
    event_data = {
        'type': 'network_joined',
        'peer_id': peer_id,
        'created_by': peer_shared_id,
        'inviter_peer_shared_id': inviter_peer_shared_id,
        'created_at': t_ms
    }

    # Get joiner's private key for signing
    private_key = peer.get_private_key(peer_id, peer_id, db)
    if not private_key:
        log.error(f"network_joined.create() no private key for peer {peer_id[:20]}...")
        raise ValueError(f"No private key for peer {peer_id}")

    # Sign the event
    signed_event = crypto.sign_event(event_data, private_key)

    # Store as signed plaintext
    canonical = crypto.canonicalize_json(signed_event)
    network_joined_id = store.event(canonical, peer_id, t_ms, db)

    log.info(f"network_joined.create() created event {network_joined_id[:20]}... for peer {peer_id[:20]}...")

    return network_joined_id


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project network_joined event into network_joiners table.

    Marks this peer as joined network (bootstrap complete).
    """
    unsafedb = create_unsafe_db(db)

    # Get blob from store
    blob = store.get(event_id, unsafedb)
    if not blob:
        log.warning(f"network_joined.project() blob not found for event_id={event_id}")
        return None

    # Parse JSON (signed plaintext)
    event_data = crypto.parse_json(blob)

    peer_id = event_data.get('peer_id')
    inviter_peer_shared_id = event_data.get('inviter_peer_shared_id')

    if not peer_id or not inviter_peer_shared_id:
        log.warning(f"network_joined.project() missing peer_id or inviter_peer_shared_id")
        return None

    # Only project if this is our own network_joined event
    if recorded_by != peer_id:
        log.debug(f"network_joined.project() skipping foreign network_joined event")
        return event_id

    # Mark this peer as joined network (subjective table, use safedb)
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO network_joiners (peer_id, recorded_by) VALUES (?, ?)""",
        (peer_id, recorded_by)
    )

    log.info(f"network_joined.project() marked {peer_id[:20]}... as joined network")

    return event_id
